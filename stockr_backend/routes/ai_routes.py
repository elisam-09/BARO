# ── IA de BARO : payée par le propriétaire de l'app, jamais par le commerçant ──
#
# Deux usages :
#   * Spectra  — reconnaître les produits sur une photo, en préférant les
#                noms du catalogue du commerçant ;
#   * Assistant — répondre sur les données du commerce, dans sa langue.
#
# La clé reste sur ce serveur : le navigateur ne la voit jamais. Le
# fournisseur se choisit dans stockr_backend/.env :
#   ANTHROPIC_API_KEY  -> Claude   (modèle : AI_MODEL, par défaut claude-opus-5-5 ;
#                                   AI_MODEL_VISION / AI_MODEL_CHAT pour un modèle par usage)
#   GEMINI_API_KEY     -> Gemini   (offre gratuite de Google, en secours)
# Sans aucune clé, /status le dit, et l'app garde ses moteurs locaux
# (code-barres, lecture du texte, reconnaissance hors ligne).
#
# Chaque commerçant a un quota quotidien selon son forfait : c'est ce qui
# borne la facture du propriétaire, quel que soit le nombre d'utilisateurs.
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime

from flask import Blueprint, jsonify, request

from models import db, token_required

ai_bp = Blueprint('ai', __name__)


class AiUsage(db.Model):
    __tablename__ = 'ai_usage'

    id      = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    day     = db.Column(db.String(10), nullable=False, index=True)
    kind    = db.Column(db.String(10), nullable=False)          # vision | chat
    count   = db.Column(db.Integer, nullable=False, default=0)


# Quotas par jour et par forfait. Réglables sans toucher au code :
# AI_QUOTA_PRO_VISION=100, par exemple.
_QUOTAS = {
    'free':       {'vision': 3,   'chat': 5},
    'starter':    {'vision': 15,  'chat': 20},
    'pro':        {'vision': 60,  'chat': 80},
    'enterprise': {'vision': 300, 'chat': 400},
}

_MODELES_AVEC_REPLI = ('claude-opus-5-5', 'claude-opus-5', 'claude-sonnet-5-5', 'claude-fable-5-1')

_MAX_IMAGE_B64 = 2_500_000      # ~1,8 Mo d'image : l'app réduit à 1024 px avant l'envoi
_MAX_TEXTE = 12_000


def _fournisseur():
    if os.environ.get('ANTHROPIC_API_KEY'):
        return 'claude'
    if os.environ.get('GEMINI_API_KEY'):
        return 'gemini'
    return None


def _forfait(user):
    plan = (getattr(user, 'plan', None) or 'free').lower()
    fin = getattr(user, 'plan_expires', None)
    if plan != 'free' and fin and fin < datetime.utcnow():
        return 'free'
    return plan if plan in _QUOTAS else 'free'


def _quota(user, kind):
    plan = _forfait(user)
    defaut = _QUOTAS[plan][kind]
    try:
        return int(os.environ.get(f'AI_QUOTA_{plan.upper()}_{kind.upper()}', defaut))
    except ValueError:
        return defaut


def _usage(user, kind):
    jour = datetime.utcnow().strftime('%Y-%m-%d')
    u = AiUsage.query.filter_by(user_id=user.id, day=jour, kind=kind).first()
    return u, jour


def _compter(user, kind):
    u, jour = _usage(user, kind)
    if not u:
        u = AiUsage(user_id=user.id, day=jour, kind=kind, count=0)
        db.session.add(u)
    u.count = (u.count or 0) + 1
    db.session.commit()


def _reste(user, kind):
    u, _ = _usage(user, kind)
    return max(0, _quota(user, kind) - (u.count if u else 0))


# ── Claude ──────────────────────────────────────────────────────────────
def _modele_claude(genre):
    # Un modèle par usage si le propriétaire le veut : reconnaître une
    # bouteille de Coca n'a pas besoin du modèle le plus cher.
    return (os.environ.get('AI_MODEL_' + genre.upper())
            or os.environ.get('AI_MODEL')
            or 'claude-opus-5-5')


def _claude(systeme, messages, max_tokens, genre):
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'], timeout=60.0, max_retries=1)
    modele = _modele_claude(genre)
    args = dict(
        model=modele,
        max_tokens=max_tokens,
        system=systeme,
        messages=messages,
        # Une réponse de commerce n'a pas besoin d'une longue réflexion :
        # l'effort bas garde la réponse rapide et la facture basse.
        output_config={'effort': os.environ.get('AI_EFFORT', 'low')},
    )
    if modele in _MODELES_AVEC_REPLI:
        # Si le modèle décline par prudence, l'API relance la même demande
        # sur un modèle de repli, dans le même appel.
        rep = client.beta.messages.create(betas=['server-side-fallback-2026-07-01'], fallbacks='default', **args)
    else:
        rep = client.messages.create(**args)
    if rep.stop_reason == 'refusal':
        return None, 'refus'
    texte = ''.join(b.text for b in rep.content if getattr(b, 'type', '') == 'text')
    return texte, None


# ── Gemini (secours gratuit) ────────────────────────────────────────────
_GEMINI_CACHE = {'modele': None, 'quand': 0}


def _gemini_modele(cle):
    # Les noms de modèles changent tous les quelques mois : on les découvre au
    # lieu de les écrire en dur (c'est ce qui avait cassé les modèles de l'app).
    if _GEMINI_CACHE['modele'] and time.time() - _GEMINI_CACHE['quand'] < 86400:
        return _GEMINI_CACHE['modele']
    impose = os.environ.get('GEMINI_MODEL')
    if impose:
        return impose
    with urllib.request.urlopen(
            f'https://generativelanguage.googleapis.com/v1beta/models?key={cle}&pageSize=200', timeout=20) as r:
        donnees = json.loads(r.read().decode('utf-8'))
    exclus = ('thinking', 'tts', 'image', 'live', 'embedding', 'audio', 'vision-preview', 'exp')
    candidats = []
    for m in donnees.get('models', []):
        nom = m.get('name', '')
        if 'generateContent' not in (m.get('supportedGenerationMethods') or []):
            continue
        if 'flash' not in nom or any(x in nom for x in exclus):
            continue
        candidats.append(nom.split('/')[-1])
    if not candidats:
        raise RuntimeError('aucun modèle Gemini disponible')
    if 'gemini-flash-latest' in candidats:
        choix = 'gemini-flash-latest'      # alias tenu à jour par Google
    else:
        # Une version stable d'abord (une « preview » peut disparaître du jour
        # au lendemain), la plus haute, et complète plutôt que « lite ».
        def rang(nom):
            m = re.search(r'gemini-(\d+(?:\.\d+)?)', nom)
            return ('preview' not in nom, float(m.group(1)) if m else 0, 'lite' not in nom, -len(nom))
        choix = max(candidats, key=rang)
    _GEMINI_CACHE.update(modele=choix, quand=time.time())
    return choix


def _gemini(systeme, messages, max_tokens):
    cle = os.environ['GEMINI_API_KEY']
    modele = _gemini_modele(cle)
    contenus = []
    for m in messages:
        parts = []
        contenu = m['content']
        if isinstance(contenu, str):
            parts.append({'text': contenu})
        else:
            for bloc in contenu:
                if bloc.get('type') == 'text':
                    parts.append({'text': bloc['text']})
                elif bloc.get('type') == 'image':
                    src = bloc['source']
                    parts.append({'inline_data': {'mime_type': src['media_type'], 'data': src['data']}})
        contenus.append({'role': 'model' if m['role'] == 'assistant' else 'user', 'parts': parts})
    corps = {
        'contents': contenus,
        'systemInstruction': {'parts': [{'text': systeme}]},
        # Les modèles Gemini récents réfléchissent avant de répondre, et cette
        # réflexion se prend sur le même plafond : on laisse de la marge.
        'generationConfig': {'temperature': 0.3, 'maxOutputTokens': max(max_tokens, 8192)},
    }
    req = urllib.request.Request(
        f'https://generativelanguage.googleapis.com/v1beta/models/{modele}:generateContent?key={cle}',
        data=json.dumps(corps).encode('utf-8'), headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=60) as r:
        rep = json.loads(r.read().decode('utf-8'))
    cands = rep.get('candidates') or []
    if not cands:
        return None, 'refus'
    texte = ''.join(p.get('text', '') for p in (cands[0].get('content') or {}).get('parts', []))
    if not texte.strip():
        return None, 'refus'
    return texte, None


def _appeler(systeme, messages, max_tokens, genre):
    f = _fournisseur()
    if f == 'claude':
        return _claude(systeme, messages, max_tokens, genre)
    if f == 'gemini':
        return _gemini(systeme, messages, max_tokens)
    return None, 'indisponible'


# ── Routes ──────────────────────────────────────────────────────────────
@ai_bp.route('/status', methods=['GET'])
@token_required
def statut(current_user):
    f = _fournisseur()
    return jsonify({
        'available': bool(f),
        'provider': f,
        'plan': _forfait(current_user),
        'vision': {'left': _reste(current_user, 'vision'), 'max': _quota(current_user, 'vision')},
        'chat':   {'left': _reste(current_user, 'chat'),   'max': _quota(current_user, 'chat')},
    })


_PROMPT_VISION = (
    "You are Spectra, a product recognition engine for small African retail shops (the BARO app). "
    "Identify every commercial product visible in the photo. Use precise commercial names: "
    "brand + model + variant + size (e.g. \"Coca-Cola 33cl\", \"Huile Dinor 1L\", \"Riz Maman 5kg\", "
    "\"Samsung Galaxy A15\"). A device with a screen is a phone or a tablet, never a remote. "
    "Count the units you can see. "
    "Return STRICTLY a JSON array, no markdown: "
    "[{\"exact_name\":\"...\",\"brand\":\"...\",\"category\":\"drink\",\"quantity\":1,"
    "\"bbox\":[x,y,width,height],\"confidence\":0-100,\"catalog_match\":\"...\"}] "
    "with bbox as percentages 0-100. Categories: smartphone, laptop, tablet, audio, tv, appliance, gaming, "
    "drink, food, hygiene, household, beauty, clothing, shoes, bag, tool, stationery, health, toy, auto, other."
)


@ai_bp.route('/vision', methods=['POST'])
@token_required
def vision(current_user):
    if not _fournisseur():
        return jsonify({'error': 'indisponible'}), 503
    if _reste(current_user, 'vision') <= 0:
        return jsonify({'error': 'quota', 'max': _quota(current_user, 'vision')}), 429
    d = request.get_json(silent=True) or {}
    image = d.get('image') or ''
    if not image or len(image) > _MAX_IMAGE_B64:
        return jsonify({'error': 'image'}), 400
    # Le catalogue du commerçant : si un produit y figure, la réponse en
    # reprend le nom exact. C'est ce qui évite les doublons dans le stock.
    catalogue = [str(x)[:80] for x in (d.get('catalog') or [])[:300] if x]
    consigne = 'Identify the products in this photo.'
    if catalogue:
        consigne += (' If a product matches one of the shop\'s catalog names below, set "catalog_match" '
                     'to that exact name; otherwise leave it empty.\nCatalog: ' + ' | '.join(catalogue))
    messages = [{'role': 'user', 'content': [
        {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': image}},
        {'type': 'text', 'text': consigne},
    ]}]
    try:
        texte, err = _appeler(_PROMPT_VISION, messages, 2000, 'vision')
    except Exception as e:  # réseau, clé invalide, fournisseur en panne
        return jsonify({'error': 'fournisseur', 'detail': type(e).__name__}), 502
    if err:
        return jsonify({'error': err}), 422
    _compter(current_user, 'vision')
    return jsonify({'text': texte, 'provider': _fournisseur(), 'left': _reste(current_user, 'vision')})


def _prompt_assistant(langue, contexte):
    if langue == 'en':
        regle = 'Always answer in English.'
    else:
        regle = 'Réponds toujours en français simple.'
    return (
        "You are Sova, the business assistant built into BARO, a stock and sales app for small shops in "
        "West Africa. The shopkeeper is not a technician: be concrete, warm and brief (3 to 6 sentences "
        "unless asked for detail). " + regle + " "
        "Use ONLY the real data below; if something is not in it, say so instead of guessing. "
        "Money is in the currency shown in the data. "
        "When a decision would lose money — selling below cost, a discount that kills the margin, "
        "ordering stock that does not sell, a cash gap — say it plainly, with the numbers, before anything else. "
        "Suggest one or two concrete actions when relevant.\n\n"
        "REAL SHOP DATA:\n" + contexte[:_MAX_TEXTE]
    )


@ai_bp.route('/chat', methods=['POST'])
@token_required
def chat(current_user):
    if not _fournisseur():
        return jsonify({'error': 'indisponible'}), 503
    if _reste(current_user, 'chat') <= 0:
        return jsonify({'error': 'quota', 'max': _quota(current_user, 'chat')}), 429
    d = request.get_json(silent=True) or {}
    brut = d.get('messages') or []
    messages = []
    total = 0
    for m in brut[-12:]:
        role = 'assistant' if m.get('role') == 'assistant' else 'user'
        contenu = str(m.get('content') or '')[:2000]
        total += len(contenu)
        if contenu:
            messages.append({'role': role, 'content': contenu})
    # L'API attend une conversation qui commence par l'utilisateur et alterne.
    while messages and messages[0]['role'] != 'user':
        messages.pop(0)
    if not messages or messages[-1]['role'] != 'user' or total > _MAX_TEXTE:
        return jsonify({'error': 'messages'}), 400
    try:
        texte, err = _appeler(_prompt_assistant(d.get('lang') or 'fr', str(d.get('context') or '')), messages, 1200, 'chat')
    except Exception as e:
        return jsonify({'error': 'fournisseur', 'detail': type(e).__name__}), 502
    if err:
        return jsonify({'error': err}), 422
    _compter(current_user, 'chat')
    return jsonify({'text': texte, 'provider': _fournisseur(), 'left': _reste(current_user, 'chat')})
