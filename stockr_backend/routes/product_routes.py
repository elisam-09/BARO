from flask import Blueprint, request, jsonify
from sqlalchemy import delete as sql_delete
from models import db, Product, Article, product_articles, token_required


product_bp = Blueprint('product', __name__)

@product_bp.route('/', methods=['GET'])
@token_required
def get_products(current_user):
    """Récupère uniquement les produits de l'utilisateur connecté"""
    products = Product.query.filter_by(user_id=current_user.id).all()
    return jsonify([p.to_dict() for p in products])

@product_bp.route('/', methods=['POST'])
@token_required
def create_product(current_user):
    """Crée un produit pour l'utilisateur connecté"""
    data = request.json
    product = Product(
        name=data['name'],
        price=data.get('price', 0),
        purchase_price=data.get('purchase_price', 0) or 0,
        photo_url=data.get('photo_url'),
        user_id=current_user.id  # ← LIAISON IMPORTANTE
    )
    
    db.session.add(product)
    db.session.commit()
    
    if 'composition' in data:
        for comp in data['composition']:
            article = Article.query.filter_by(
                id=comp['article_id'], 
                user_id=current_user.id  # Vérifie que l'article appartient à l'utilisateur
            ).first()
            if article:
                product.composition.append(article)
                stmt = product_articles.update().where(
                    product_articles.c.product_id == product.id,
                    product_articles.c.article_id == article.id
                ).values(quantity_used=comp.get('quantity_used', 1.0))
                db.session.execute(stmt)
    
    db.session.commit()
    return jsonify(product.to_dict()), 201

@product_bp.route('/<int:product_id>', methods=['DELETE'])
@token_required
def delete_product(current_user, product_id):
    product = Product.query.filter_by(id=product_id, user_id=current_user.id).first_or_404()
    db.session.delete(product)
    db.session.commit()
    return jsonify({'message': 'Produit supprimé'}), 200

@product_bp.route('/<int:product_id>', methods=['PUT'])
@token_required
def update_product(current_user, product_id):
    product = Product.query.filter_by(id=product_id, user_id=current_user.id).first_or_404()
    data = request.json
    if 'name' in data: product.name = data['name']
    if 'price' in data: product.price = data['price']
    if 'purchase_price' in data: product.purchase_price = data['purchase_price'] or 0
    if 'composition' in data:
        # Vider via l'ORM pour garder le cache SQLAlchemy cohérent
        product.composition.clear()
        db.session.flush()
        for comp in data['composition']:
            article = Article.query.filter_by(
                id=comp['article_id'],
                user_id=current_user.id
            ).first()
            if article:
                product.composition.append(article)
                db.session.flush()
                stmt = product_articles.update().where(
                    product_articles.c.product_id == product.id,
                    product_articles.c.article_id == article.id
                ).values(quantity_used=comp.get('quantity_used', 1.0))
                db.session.execute(stmt)
    db.session.commit()
    return jsonify(product.to_dict()), 200