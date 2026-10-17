# ── Double authentification réelle (code d'application, RFC 6238) ──
#
# L'ancienne « 2FA » tirait le code dans le navigateur, après une connexion
# que le serveur avait déjà acceptée, et l'affichait à l'écran quand l'e-mail
# n'était pas configuré : elle ne protégeait rien. Ici, c'est le serveur qui
# garde le secret et qui refuse d'ouvrir la session sans le bon code.
#
# Aucun service d'e-mail ni de SMS n'est nécessaire : le code vient d'une
# application d'authentification (Google Authenticator, Microsoft
# Authenticator, Authy…), sur le téléphone du commerçant.
#
# Connexion en deux temps :
#   1. /api/auth/login (mot de passe juste) → {need_2fa, ticket} sans session ;
#   2. /api/auth/2fa/login {ticket, code}   → la session.
# Le ticket est un jeton aléatoire gardé en base, valable 5 minutes : il ne
# dépend pas de SECRET_KEY, que l'on ne peut pas supposer configurée.
import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import time
import urllib.parse
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

from models import db
from models.user import User
from routes.auth_routes import token_required

twofa_bp = Blueprint('twofa', __name__)

_PAS = 30                 # secondes par code
_FENETRE = 1              # tolérance : un pas avant, un pas après (horloge du téléphone)
_ESSAIS = 5               # essais faux avant blocage
_BLOCAGE = timedelta(minutes=15)
_TICKET = timedelta(minutes=5)
_NB_SECOURS = 8


def _code_totp(secret_b32, pas):
    cle = base64.b32decode(secret_b32 + '=' * (-len(secret_b32) % 8), casefold=True)
    h = hmac.new(cle, struct.pack('>Q', pas), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    n = (struct.unpack('>I', h[o:o + 4])[0] & 0x7FFFFFFF) % 1000000
    return f'{n:06d}'


def _pas_valide(secret_b32, code, dernier):
    """Le pas de temps du code s'il est juste (et pas déjà utilisé), sinon None."""
    code = ''.join(c for c in str(code or '') if c.isdigit())
    if len(code) != 6 or not secret_b32:
        return None
    maintenant = int(time.time()) // _PAS
    for d in range(-_FENETRE, _FENETRE + 1):
        pas = maintenant + d
        if dernier is not None and pas <= dernier:
            continue          # un code déjà servi ne rouvre pas une session
        if hmac.compare_digest(_code_totp(secret_b32, pas), code):
            return pas
    return None


def _hache(code):
    return hashlib.sha256(code.strip().upper().replace('-', '').encode()).hexdigest()


def _bloque(user):
    return user.totp_lock_until and user.totp_lock_until > datetime.utcnow()


def _echec(user):
    user.totp_fails = (user.totp_fails or 0) + 1
    if user.totp_fails >= _ESSAIS:
        user.totp_lock_until = datetime.utcnow() + _BLOCAGE
        user.totp_fails = 0
    db.session.commit()


def _verifier(user, code):
    """Code d'application ou code de secours (usage unique). True si accepté."""
    pas = _pas_valide(user.totp_secret, code, user.totp_last_step)
    if pas is not None:
        user.totp_last_step = pas
        user.totp_fails = 0
        db.session.commit()
        return True
    brut = str(code or '').strip()
    if len(brut.replace('-', '')) == 10:
        try:
            reste = json.loads(user.totp_recovery or '[]')
        except ValueError:
            reste = []
        h = _hache(brut)
        if h in reste:
            reste.remove(h)
            user.totp_recovery = json.dumps(reste)
            user.totp_fails = 0
            db.session.commit()
            return True
    _echec(user)
    return False


def ticket_pour(user):
    """Appelé par la connexion (mot de passe, Google, Apple) quand la 2FA est active."""
    user.totp_ticket = secrets.token_urlsafe(32)
    user.totp_ticket_expiry = datetime.utcnow() + _TICKET
    db.session.commit()
    return {'need_2fa': True, 'ticket': user.totp_ticket}


@twofa_bp.route('/status', methods=['GET'])
@token_required
def statut(current_user):
    try:
        reste = len(json.loads(current_user.totp_recovery or '[]'))
    except ValueError:
        reste = 0
    return jsonify({'enabled': bool(current_user.totp_enabled), 'recovery_left': reste})


@twofa_bp.route('/setup', methods=['POST'])
@token_required
def preparer(current_user):
    if current_user.totp_enabled:
        return jsonify({'error': 'deja_active'}), 409
    secret = base64.b32encode(os.urandom(20)).decode().rstrip('=')
    current_user.totp_secret = secret
    current_user.totp_last_step = None
    db.session.commit()
    etiquette = urllib.parse.quote(f'BARO:{current_user.email}')
    uri = f'otpauth://totp/{etiquette}?secret={secret}&issuer=BARO&digits=6&period={_PAS}'
    return jsonify({'secret': secret, 'uri': uri})


@twofa_bp.route('/enable', methods=['POST'])
@token_required
def activer(current_user):
    if current_user.totp_enabled:
        return jsonify({'error': 'deja_active'}), 409
    if _bloque(current_user):
        return jsonify({'error': 'bloque'}), 429
    code = (request.get_json(silent=True) or {}).get('code')
    pas = _pas_valide(current_user.totp_secret, code, None)
    if pas is None:
        _echec(current_user)
        return jsonify({'error': 'code'}), 400
    secours = []
    for _ in range(_NB_SECOURS):
        brut = secrets.token_hex(5).upper()
        secours.append(brut[:5] + '-' + brut[5:])
    current_user.totp_enabled = True
    current_user.totp_last_step = pas
    current_user.totp_fails = 0
    current_user.totp_recovery = json.dumps([_hache(c) for c in secours])
    db.session.commit()
    # Les codes de secours ne sont montrés qu'une fois : seule leur empreinte est gardée.
    return jsonify({'enabled': True, 'recovery_codes': secours})


@twofa_bp.route('/disable', methods=['POST'])
@token_required
def desactiver(current_user):
    if not current_user.totp_enabled:
        return jsonify({'enabled': False})
    if _bloque(current_user):
        return jsonify({'error': 'bloque'}), 429
    code = (request.get_json(silent=True) or {}).get('code')
    if not _verifier(current_user, code):
        return jsonify({'error': 'code'}), 400
    current_user.totp_enabled = False
    current_user.totp_secret = None
    current_user.totp_recovery = None
    current_user.totp_last_step = None
    db.session.commit()
    return jsonify({'enabled': False})


@twofa_bp.route('/login', methods=['POST'])
def connexion():
    d = request.get_json(silent=True) or {}
    ticket = str(d.get('ticket') or '')
    if len(ticket) < 20:
        return jsonify({'error': 'ticket'}), 400
    user = User.query.filter_by(totp_ticket=ticket).first()
    if not user or not user.totp_enabled or not user.totp_ticket_expiry \
            or user.totp_ticket_expiry < datetime.utcnow():
        return jsonify({'error': 'ticket'}), 401
    if _bloque(user):
        return jsonify({'error': 'bloque'}), 429
    if not _verifier(user, d.get('code')):
        return jsonify({'error': 'code'}), 401
    user.totp_ticket = None
    user.totp_ticket_expiry = None
    user.last_login = datetime.utcnow()
    user.auth_token = secrets.token_urlsafe(32)
    user.token_expiry = datetime.utcnow() + timedelta(days=30)
    db.session.commit()
    return jsonify({'user': user.to_dict(include_token=True)})
