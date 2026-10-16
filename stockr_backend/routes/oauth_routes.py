# ── Connexion avec Google ou Apple, vérifiée par le serveur ──
#
# Le navigateur obtient un jeton auprès de Google ou d'Apple ; c'est le
# serveur qui le vérifie (signature, destinataire, adresse vérifiée) avant
# d'ouvrir ou de créer le compte. Un jeton émis pour une autre application,
# expiré, ou une adresse non vérifiée sont refusés.
#
# Configuration dans stockr_backend/.env :
#   GOOGLE_CLIENT_ID  -> identifiant OAuth « Application Web » (Google Cloud)
#   APPLE_SERVICE_ID  -> Services ID « Sign in with Apple » (compte Apple
#                        Developer requis) ; vérification avec PyJWT[crypto]
# Sans ces valeurs, /config le dit et l'app n'affiche pas les boutons.
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

from models import db
from models.user import User

oauth_bp = Blueprint('oauth', __name__)

_DEVISES = {'XOF', 'XAF', 'EUR', 'USD', 'GNF', 'NGN', 'GHS', 'MAD', 'CDF', 'KES', 'GBP', 'CAD', 'CHF'}


def _lire_json(url, entetes=None, delai=15):
    req = urllib.request.Request(url, headers=entetes or {})
    with urllib.request.urlopen(req, timeout=delai) as r:
        return json.loads(r.read().decode('utf-8'))


@oauth_bp.route('/config', methods=['GET'])
def config():
    return jsonify({
        'google_client_id': os.environ.get('GOOGLE_CLIENT_ID') or None,
        'apple_service_id': os.environ.get('APPLE_SERVICE_ID') or None,
    })


def _ouvrir_compte(email, nom, d):
    """Connecte le compte de cette adresse, ou le crée. Renvoie (user, nouveau)."""
    email = email.strip().lower()
    user = User.query.filter(db.func.lower(User.email) == email).first()
    nouveau = user is None
    if nouveau:
        devise = str(d.get('currency') or 'XOF').upper()
        user = User(
            email=email,
            name=(nom or email.split('@')[0])[:100],
            business_name=(str(d.get('business_name') or '').strip() or nom or email.split('@')[0])[:100],
            language='en' if d.get('language') == 'en' else 'fr',
            currency=devise if devise in _DEVISES else 'XOF',
            country=str(d.get('country') or 'CI')[:50],
        )
        # Aucun mot de passe connu de personne : ce compte s'ouvre par
        # Google/Apple (ou après « mot de passe oublié »).
        user.set_password(secrets.token_urlsafe(32))
        db.session.add(user)
    user.last_login = datetime.utcnow()
    user.auth_token = secrets.token_urlsafe(32)
    user.token_expiry = datetime.utcnow() + timedelta(days=30)
    db.session.commit()
    return user, nouveau


@oauth_bp.route('/google', methods=['POST'])
def google():
    client_id = os.environ.get('GOOGLE_CLIENT_ID')
    if not client_id:
        return jsonify({'error': 'non_configure'}), 503
    d = request.get_json(silent=True) or {}
    jeton_acces = str(d.get('access_token') or '')
    jeton_id = str(d.get('credential') or d.get('id_token') or '')
    if not jeton_acces and not jeton_id:
        return jsonify({'error': 'jeton'}), 400
    try:
        if jeton_id:
            info = _lire_json('https://oauth2.googleapis.com/tokeninfo?id_token=' + urllib.parse.quote(jeton_id))
            if info.get('iss') not in ('accounts.google.com', 'https://accounts.google.com'):
                return jsonify({'error': 'jeton'}), 401
            destinataire = info.get('aud')
        else:
            info = _lire_json('https://oauth2.googleapis.com/tokeninfo?access_token=' + urllib.parse.quote(jeton_acces))
            destinataire = info.get('aud') or info.get('azp')
    except urllib.error.HTTPError:
        return jsonify({'error': 'jeton'}), 401          # Google refuse le jeton
    except Exception as e:
        return jsonify({'error': 'google', 'detail': type(e).__name__}), 502
    # Un jeton émis pour une autre application ne doit pas ouvrir de compte ici.
    if destinataire != client_id:
        return jsonify({'error': 'jeton'}), 401
    if str(info.get('email_verified')).lower() != 'true' or not info.get('email'):
        return jsonify({'error': 'email_non_verifie'}), 401
    try:
        if int(info.get('exp', 0)) < int(datetime.utcnow().timestamp()):
            return jsonify({'error': 'jeton'}), 401
    except (TypeError, ValueError):
        return jsonify({'error': 'jeton'}), 401
    nom = info.get('name') or ''
    if not nom and jeton_acces:
        try:
            profil = _lire_json('https://www.googleapis.com/oauth2/v3/userinfo',
                                {'Authorization': 'Bearer ' + jeton_acces})
            nom = profil.get('name') or profil.get('given_name') or ''
        except Exception:
            nom = ''
    user, nouveau = _ouvrir_compte(info['email'], nom, d)
    return jsonify({'user': user.to_dict(include_token=True), 'nouveau': nouveau})


@oauth_bp.route('/apple', methods=['POST'])
def apple():
    service_id = os.environ.get('APPLE_SERVICE_ID')
    if not service_id:
        return jsonify({'error': 'non_configure'}), 503
    try:
        import jwt  # PyJWT[crypto]
    except ImportError:
        return jsonify({'error': 'non_configure', 'detail': 'pip install --user "PyJWT[crypto]"'}), 503
    d = request.get_json(silent=True) or {}
    jeton = str(d.get('id_token') or '')
    if not jeton:
        return jsonify({'error': 'jeton'}), 400
    try:
        cle = jwt.PyJWKClient('https://appleid.apple.com/auth/keys').get_signing_key_from_jwt(jeton)
        info = jwt.decode(jeton, cle.key, algorithms=['RS256'], audience=service_id,
                          issuer='https://appleid.apple.com')
    except jwt.PyJWTError:
        return jsonify({'error': 'jeton'}), 401
    except Exception as e:
        return jsonify({'error': 'apple', 'detail': type(e).__name__}), 502
    email = info.get('email')
    if not email or str(info.get('email_verified', 'true')).lower() not in ('true', '1'):
        return jsonify({'error': 'email_non_verifie'}), 401
    # Apple ne donne le nom qu'à la première connexion, côté navigateur.
    user, nouveau = _ouvrir_compte(email, str(d.get('name') or '').strip(), d)
    return jsonify({'user': user.to_dict(include_token=True), 'nouveau': nouveau})
