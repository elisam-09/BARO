from flask import Flask, jsonify
from flask_cors import CORS
from config import Config
from models import db

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    
    CORS(app)
    db.init_app(app)
    
    # Importer tous les modèles avant create_all pour que SQLAlchemy connaisse les tables
    from routes.client_routes import Client  # noqa: F401
    from routes.expense_routes import Expense  # noqa: F401
    from routes.store_routes import StoreBlob  # noqa: F401
    from routes.order_routes import ShopOrder  # noqa: F401
    from routes.push_routes import PushSub  # noqa: F401

    with app.app_context():
        db.create_all()
        # Migration légère : ajoute les colonnes d'abonnement aux bases existantes
        # (create_all ne modifie pas les tables déjà créées). Sans effet si présentes.
        from sqlalchemy import text
        for col, ddl in [
            ('plan', "VARCHAR(20) DEFAULT 'free'"),
            ('plan_status', 'VARCHAR(20)'),
            ('plan_expires', 'TIMESTAMP'),
            ('billing_provider', 'VARCHAR(20)'),
            ('billing_customer_id', 'VARCHAR(120)'),
            ('team_code', 'VARCHAR(20)'),
        ]:
            try:
                db.session.execute(text(f'ALTER TABLE "user" ADD COLUMN {col} {ddl}'))
                db.session.commit()
            except Exception:
                db.session.rollback()
        # Même chose pour les métadonnées métier des articles (sync multi-appareils)
        for col, ddl in [
            ('ref', 'VARCHAR(80)'),
            ('price', 'FLOAT DEFAULT 0'),
            ('purchase_price', 'FLOAT DEFAULT 0'),
            ('sell_price', 'FLOAT DEFAULT 0'),
            ('category', 'VARCHAR(80)'),
            ('ean', 'VARCHAR(40)'),
            ('expiry', 'VARCHAR(20)'),
            ('perishable', 'BOOLEAN'),
            ('description', 'VARCHAR(1000)'),
            ('in_boutique', 'BOOLEAN'),
            ('variants', 'TEXT'),
        ]:
            try:
                db.session.execute(text(f'ALTER TABLE article ADD COLUMN {col} {ddl}'))
                db.session.commit()
            except Exception:
                db.session.rollback()
        # Coût de revient des produits composés
        try:
            db.session.execute(text('ALTER TABLE product ADD COLUMN purchase_price FLOAT DEFAULT 0'))
            db.session.commit()
        except Exception:
            db.session.rollback()
        # Suivi de commande côté client (code secret public)
        try:
            db.session.execute(text('ALTER TABLE shop_order ADD COLUMN track_code VARCHAR(16)'))
            db.session.commit()
        except Exception:
            db.session.rollback()

    from routes.article_routes import article_bp
    from routes.product_routes import product_bp
    from routes.sale_routes import sale_bp
    from routes.alert_routes import alert_bp
    from routes.auth_routes import auth_bp
    from routes.prediction_routes import prediction_bp
    from routes.spectra_routes import spectra_bp
    from routes.client_routes import client_bp
    from routes.billing_routes import billing_bp
    from routes.expense_routes import expense_bp
    from routes.store_routes import store_bp
    from routes.order_routes import order_bp
    from routes.team_routes import team_bp
    from routes.push_routes import push_bp
    from routes.shopper_routes import shopper_bp

    app.register_blueprint(article_bp, url_prefix='/api/articles')
    app.register_blueprint(product_bp, url_prefix='/api/products')
    app.register_blueprint(sale_bp, url_prefix='/api/sales')
    app.register_blueprint(alert_bp, url_prefix='/api/alerts')
    app.register_blueprint(auth_bp, url_prefix='/api/auth')
    app.register_blueprint(prediction_bp, url_prefix='/api/predictions')
    app.register_blueprint(spectra_bp, url_prefix='/api/spectra')
    app.register_blueprint(client_bp, url_prefix='/api/clients')
    app.register_blueprint(billing_bp, url_prefix='/api/billing')
    app.register_blueprint(expense_bp, url_prefix='/api/expenses')
    app.register_blueprint(store_bp, url_prefix='/api/store')
    app.register_blueprint(order_bp, url_prefix='/api/orders')
    app.register_blueprint(team_bp, url_prefix='/api/team')
    app.register_blueprint(push_bp, url_prefix='/api/push')
    app.register_blueprint(shopper_bp, url_prefix='/api/shoppers')
    
    
    @app.route('/api/health')
    def health():
        return jsonify({"status": "healthy"})
    
    return app

# Expose app at module level pour gunicorn (app:app)
app = create_app()

if __name__ == '__main__':
    app.run(debug=True, port=5001)