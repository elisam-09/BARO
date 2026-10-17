from . import db
from datetime import datetime
import bcrypt

class User(db.Model):
    __tablename__ = 'user'
    
    id = db.Column(db.Integer, primary_key=True)
    
    # Authentification
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    
    # Informations personnelles
    name = db.Column(db.String(100), nullable=False)
    business_name = db.Column(db.String(100), nullable=False)
    
    # Photo de profil (optionnelle)
    profile_photo_url = db.Column(db.String(500), nullable=True)
    
    # Préférences
    language = db.Column(db.String(10), nullable=False, default='fr')
    currency = db.Column(db.String(3), nullable=False, default='XOF')
    country = db.Column(db.String(50), nullable=False, default='CI')
    
    # Métadonnées
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_login = db.Column(db.DateTime, nullable=True)
    
    # Token de session (pour mobile)
    auth_token = db.Column(db.String(500), nullable=True)
    token_expiry = db.Column(db.DateTime, nullable=True)

    # Abonnement (activé uniquement via les webhooks de paiement vérifiés)
    plan = db.Column(db.String(20), nullable=True, default='free')
    plan_status = db.Column(db.String(20), nullable=True)          # active | cancelled | past_due
    plan_expires = db.Column(db.DateTime, nullable=True)
    billing_provider = db.Column(db.String(20), nullable=True)     # stripe | cinetpay
    billing_customer_id = db.Column(db.String(120), nullable=True)

    # Équipe : code partagé permettant aux vendeurs de rejoindre la boutique
    team_code = db.Column(db.String(20), nullable=True, index=True)

    # Double authentification (code d'application, vérifié par le serveur)
    totp_secret = db.Column(db.String(64), nullable=True)
    totp_enabled = db.Column(db.Boolean, nullable=True, default=False)
    totp_last_step = db.Column(db.Integer, nullable=True)
    totp_fails = db.Column(db.Integer, nullable=True, default=0)
    totp_lock_until = db.Column(db.DateTime, nullable=True)
    totp_recovery = db.Column(db.Text, nullable=True)
    totp_ticket = db.Column(db.String(64), nullable=True, index=True)
    totp_ticket_expiry = db.Column(db.DateTime, nullable=True)
    
    def set_password(self, password):
        """Hash le mot de passe avec bcrypt"""
        salt = bcrypt.gensalt()
        self.password_hash = bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')
    
    def check_password(self, password):
        """Vérifie le mot de passe"""
        return bcrypt.checkpw(password.encode('utf-8'), self.password_hash.encode('utf-8'))
    
    def generate_token(self):
        """Génère un token de session simple (pour MVP)"""
        import secrets
        from datetime import datetime, timedelta
        
        self.auth_token = secrets.token_urlsafe(32)
        self.token_expiry = datetime.utcnow() + timedelta(days=30)
        db.session.commit()
        return self.auth_token
    
    def to_dict(self, include_token=False):
        """Convertit l'utilisateur en dictionnaire"""
        data = {
            'id': self.id,
            'email': self.email,
            'name': self.name,
            'business_name': self.business_name,
            'profile_photo_url': self.profile_photo_url,
            'language': self.language,
            'currency': self.currency,
            'country': self.country,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'plan': self.plan or 'free',
            'plan_status': self.plan_status,
            'plan_expires': self.plan_expires.isoformat() if self.plan_expires else None,
            'two_factor': bool(self.totp_enabled),
        }
        
        if include_token and self.auth_token:
            data['auth_token'] = self.auth_token
            data['token_expiry'] = self.token_expiry.isoformat() if self.token_expiry else None
        
        return data