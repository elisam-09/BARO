from flask import Blueprint, request, jsonify
from models import db, User, Sale, Product 
from datetime import datetime
import secrets
from functools import wraps
from datetime import datetime, timedelta

auth_bp = Blueprint('auth', __name__)

# -----------------------------------
# DÉCORATEUR (directement dans le fichier)
# -----------------------------------
def token_required(f):
    """Décorateur pour vérifier le token d'authentification"""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        
        # Récupère le token du header
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]
        
        if not token:
            return jsonify({'error': 'Token manquant'}), 401
        
        # Vérifie le token
        user = User.query.filter_by(auth_token=token).first()
        
        if not user:
            return jsonify({'error': 'Token invalide'}), 401
        
        if user.token_expiry and user.token_expiry < datetime.utcnow():
            return jsonify({'error': 'Token expiré'}), 401
        
        # Passe l'utilisateur à la route
        return f(user, *args, **kwargs)
    
    return decorated

# -----------------------------------
# SERVICE D'AUTH (directement dans le fichier)
# -----------------------------------
class AuthService:
    
    @staticmethod
    def register_user(email, password, name, business_name, language='fr', currency='XOF', country='CI'):
        """Crée un nouvel utilisateur"""
        
        # Vérifie si l'email existe déjà
        existing = User.query.filter_by(email=email).first()
        if existing:
            return {'error': 'Email déjà utilisé'}, None
        
        # Crée l'utilisateur
        user = User(
            email=email,
            name=name,
            business_name=business_name,
            language=language,
            currency=currency,
            country=country
        )
        
        # Hash le mot de passe
        user.set_password(password)
        
        # Génère un token
        token = secrets.token_urlsafe(32)
        user.auth_token = token
        user.token_expiry = datetime.utcnow() + timedelta(days=30)
        
        # Sauvegarde
        db.session.add(user)
        db.session.commit()
        
        return None, {'user': user.to_dict(include_token=True)}
    
    @staticmethod
    def login_user(email, password):
        """Connecte un utilisateur"""
        
        user = User.query.filter_by(email=email).first()
        
        if not user or not user.check_password(password):
            return {'error': 'Email ou mot de passe incorrect'}, None

        # Double authentification : le mot de passe ne suffit pas, aucune
        # session n'est ouverte avant le code de l'application.
        if user.totp_enabled:
            from routes.twofa_routes import ticket_pour
            return None, ticket_pour(user)
        
        # Met à jour la dernière connexion
        user.last_login = datetime.utcnow()
        
        # Génère un nouveau token
        token = secrets.token_urlsafe(32)
        user.auth_token = token
        user.token_expiry = datetime.utcnow() + timedelta(days=30)
        
        db.session.commit()
        
        return None, {'user': user.to_dict(include_token=True)}
    
    @staticmethod
    def logout_user(token):
        """Déconnecte l'utilisateur"""
        
        user = User.query.filter_by(auth_token=token).first()
        
        if user:
            user.auth_token = None
            user.token_expiry = None
            db.session.commit()
            return True
        
        return False
    
    @staticmethod
    def update_profile(user, data):
        """Met à jour le profil"""
        
        updatable_fields = ['name', 'business_name', 'language', 'currency', 'country']
        
        for field in updatable_fields:
            if field in data:
                setattr(user, field, data[field])
        
        # Changement de mot de passe
        if 'new_password' in data and 'old_password' in data:
            if not user.check_password(data['old_password']):
                return {'error': 'Ancien mot de passe incorrect'}, None
            user.set_password(data['new_password'])
        
        db.session.commit()
        
        return None, {'user': user.to_dict()}

# -----------------------------------
# ROUTES
# -----------------------------------

@auth_bp.route('/register', methods=['POST'])
def register():
    """Crée un nouveau compte utilisateur"""
    data = request.json
    
    error, result = AuthService.register_user(
        email=data['email'],
        password=data['password'],
        name=data['name'],
        business_name=data['business_name'],
        language=data.get('language', 'fr'),
        currency=data.get('currency', 'XOF'),
        country=data.get('country', 'CI')
    )
    
    if error:
        return jsonify({'error': error}), 400
    
    return jsonify({
        'message': 'Compte créé avec succès',
        'user': result['user']
    }), 201

@auth_bp.route('/login', methods=['POST'])
def login():
    """Connecte un utilisateur"""
    data = request.json
    
    error, result = AuthService.login_user(
        email=data['email'],
        password=data['password']
    )
    
    if error:
        return jsonify({'error': error}), 401

    if result.get('need_2fa'):
        return jsonify(result)
    
    return jsonify({
        'message': 'Connexion réussie',
        'user': result['user']
    })

@auth_bp.route('/logout', methods=['POST'])
def logout():
    """Déconnecte l'utilisateur"""
    auth_header = request.headers.get('Authorization')
    
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({'error': 'Token manquant'}), 401
    
    token = auth_header.split(' ')[1]
    
    success = AuthService.logout_user(token)
    
    if success:
        return jsonify({'message': 'Déconnexion réussie'})
    else:
        return jsonify({'error': 'Token invalide'}), 401

@auth_bp.route('/profile', methods=['GET'])
@token_required
def get_profile(current_user):
    """Récupère le profil"""
    return jsonify({'user': current_user.to_dict()})

@auth_bp.route('/profile', methods=['PUT'])
@token_required
def update_profile(current_user):
    """Met à jour le profil"""
    data = request.json
    
    error, result = AuthService.update_profile(current_user, data)
    
    if error:
        return jsonify({'error': error}), 400
    
    return jsonify({
        'message': 'Profil mis à jour',
        'user': result['user']
    })

@auth_bp.route('/test', methods=['GET'])
def test():
    """Route de test"""
    return jsonify({"message": "Auth routes OK"})

@auth_bp.route('/dashboard', methods=['GET'])
@token_required
def get_dashboard(current_user):
    """Récupère les données pour le bilan"""
    from sqlalchemy import func
    from datetime import datetime, timedelta
    
    today = datetime.utcnow().date()
    
    # Ventes aujourd'hui
    today_sales = db.session.query(func.sum(Sale.quantity)).filter(
        Sale.user_id == current_user.id,
        func.date(Sale.timestamp) == today
    ).scalar() or 0
    
    # Chiffre d'affaires aujourd'hui
    today_revenue = db.session.query(
        func.sum(Sale.quantity * Product.price)
    ).join(Product).filter(
        Sale.user_id == current_user.id,
        func.date(Sale.timestamp) == today
    ).scalar() or 0
    
    # Top produits
    top_products = db.session.query(
        Product.name,
        func.sum(Sale.quantity).label('total_sold'),
        func.sum(Sale.quantity * Product.price).label('total_revenue')
    ).join(Sale).filter(
        Sale.user_id == current_user.id
    ).group_by(Product.id).order_by(
        func.sum(Sale.quantity).desc()
    ).limit(5).all()
    
    top_products_list = [
        {
            'name': p.name,
            'count': p.total_sold,
            'revenue': float(p.total_revenue)
        }
        for p in top_products
    ]
    
    return jsonify({
        'today_sales': today_sales,
        'today_revenue': float(today_revenue),
        'week_revenue': 0,  # À implémenter
        'month_revenue': 0,  # À implémenter
        'top_products': top_products_list
    })