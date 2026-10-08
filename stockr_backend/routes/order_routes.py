# ── Commandes de la boutique en ligne ──
# La vitrine publique (site généré) POST ses commandes ici → le
# propriétaire les récupère dans l'app (impossible hors-ligne).
from flask import Blueprint, request, jsonify
from models import db, token_required, User
from datetime import datetime
import json
import secrets

order_bp = Blueprint('order', __name__)

_STATUS_LABELS = {
    'pending':   'Reçue — en attente de confirmation',
    'confirmed': 'Confirmée ✅',
    'preparing': 'En préparation 👨‍🍳',
    'shipped':   'En route 🚚',
    'delivered': 'Livrée / retirée 📦',
    'cancelled': 'Annulée',
}


class ShopOrder(db.Model):
    __tablename__ = 'shop_order'

    id            = db.Column(db.Integer, primary_key=True)
    customer_name = db.Column(db.String(120), nullable=True)
    phone         = db.Column(db.String(40),  nullable=True)
    items         = db.Column(db.Text,        nullable=False, default='[]')  # JSON
    total         = db.Column(db.Float,       nullable=False, default=0)
    mode          = db.Column(db.String(20),  nullable=True)   # delivery / pickup
    zone          = db.Column(db.String(80),  nullable=True)
    payment       = db.Column(db.String(40),  nullable=True)
    note          = db.Column(db.String(500), nullable=True)
    status        = db.Column(db.String(20),  nullable=False, default='pending')
    created_at    = db.Column(db.DateTime,    default=datetime.utcnow)
    # Code secret de suivi : permet au CLIENT de consulter le statut (public)
    track_code    = db.Column(db.String(16),  nullable=True, index=True)

    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)

    def to_dict(self):
        try:
            items = json.loads(self.items or '[]')
        except (ValueError, TypeError):
            items = []
        return {
            'id':            self.id,
            'clientName':    self.customer_name,
            'phone':         self.phone,
            'items':         items,
            'total':         self.total,
            'mode':          self.mode,
            'zone':          self.zone,
            'payment':       self.payment,
            'note':          self.note,
            'status':        self.status,
            # created_at est en UTC (datetime.utcnow) : on le DIT, sinon le
            # navigateur lit une heure locale et chaque commande paraît plus
            # vieille (ou plus jeune) du décalage horaire du téléphone.
            'date':          (self.created_at.isoformat() + 'Z') if self.created_at else None,
            'source':        'online',
        }


# ── Public : la vitrine hébergée envoie une commande (pas de token) ──
@order_bp.route('/shop/<int:shop_id>', methods=['POST'])
def create_public_order(shop_id):
    owner = User.query.get(shop_id)
    if not owner:
        return jsonify({'error': 'Boutique introuvable'}), 404
    data = request.json or {}
    items = data.get('items')
    if not isinstance(items, list) or not items:
        return jsonify({'error': 'Panier vide'}), 400
    try:
        total = float(data.get('total') or 0)
    except (TypeError, ValueError):
        total = 0
    order = ShopOrder(
        customer_name = (data.get('clientName') or '').strip()[:120] or None,
        phone         = (data.get('phone') or '').strip()[:40] or None,
        items         = json.dumps(items)[:20000],
        total         = total,
        mode          = (data.get('mode') or '').strip()[:20] or None,
        zone          = (data.get('zone') or '').strip()[:80] or None,
        payment       = (data.get('payment') or '').strip()[:40] or None,
        note          = (data.get('note') or '').strip()[:500] or None,
        status        = 'pending',
        track_code    = secrets.token_urlsafe(8)[:12],
        user_id       = shop_id,
    )
    db.session.add(order)
    db.session.commit()
    # Push au patron (best-effort — la commande est déjà enregistrée)
    try:
        from routes.push_routes import notify_user
        who = order.customer_name or 'Un client'
        # La devise du commerçant, et le montant sans le tronquer : « 23 F »
        # pour une commande de 23,50 € était faux deux fois.
        owner = User.query.get(shop_id)
        devise = (owner.currency if owner and owner.currency else 'XOF')
        montant = f'{total:,.0f}' if devise in ('XOF', 'XAF', 'NGN') else f'{total:,.2f}'
        symbole = {'XOF': 'FCFA', 'XAF': 'FCFA', 'EUR': '€', 'USD': '$', 'GBP': '£'}.get(devise, devise)
        notify_user(shop_id, '🛍️ Nouvelle commande',
                    f'{who} — {montant} {symbole} ({len(items)} article(s))'.replace(',', ' '),
                    '/?view=boutique')
    except Exception:
        pass
    return jsonify({'received': True, 'id': order.id, 'track_code': order.track_code}), 201


# ── Public : le CLIENT suit sa commande (code secret requis, pas de token) ──
@order_bp.route('/track/<int:order_id>', methods=['GET'])
def track_order(order_id):
    code = (request.args.get('code') or '').strip()
    order = ShopOrder.query.get(order_id)
    if not order or not code or order.track_code != code:
        return jsonify({'error': 'Commande introuvable'}), 404
    owner = User.query.get(order.user_id)
    return jsonify({
        'status':        order.status,
        'status_label':  _STATUS_LABELS.get(order.status, order.status),
        'total':         order.total,
        'mode':          order.mode,
        'items_count':   len(json.loads(order.items or '[]')),
        'created_at':    (order.created_at.isoformat() + 'Z') if order.created_at else None,
        'business_name': owner.business_name if owner else None,
    })


# ── Propriétaire : liste de ses commandes reçues ──
@order_bp.route('/', methods=['GET'])
@token_required
def get_orders(current_user):
    orders = ShopOrder.query.filter_by(user_id=current_user.id).order_by(ShopOrder.created_at.desc()).limit(200).all()
    return jsonify([o.to_dict() for o in orders])


# ── Propriétaire : change le statut (pending → confirmed / delivered / cancelled) ──
@order_bp.route('/<int:order_id>', methods=['PUT'])
@token_required
def update_order(current_user, order_id):
    order = ShopOrder.query.filter_by(id=order_id, user_id=current_user.id).first()
    if not order:
        return jsonify({'error': 'Commande introuvable'}), 404
    status = (request.json or {}).get('status')
    # « shipped » : l'app propose désormais l'étape « en route » entre la
    # confirmation et la livraison ; le client la voit sur sa page de suivi.
    if status not in ('pending', 'confirmed', 'preparing', 'shipped', 'delivered', 'cancelled'):
        return jsonify({'error': 'Statut invalide'}), 400
    order.status = status
    db.session.commit()
    return jsonify(order.to_dict())
