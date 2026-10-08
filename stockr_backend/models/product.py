from . import db, product_articles

class Product(db.Model):
    __tablename__ = 'product'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    price = db.Column(db.Float, nullable=False, default=0)
    # Coût de revient : sans lui, l'app renvoyait le coût d'un produit
    # composé au serveur, qui l'ignorait — la modification ne tenait pas.
    purchase_price = db.Column(db.Float, nullable=True, default=0)
    photo_url = db.Column(db.String(500), nullable=True)
    
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    user = db.relationship('User', backref=db.backref('products', lazy=True))
    
    composition = db.relationship('Article', secondary=product_articles,
                                  lazy='subquery',
                                  backref=db.backref('used_in_products', lazy=True))
    
    def get_composition_details(self):
        from . import product_articles
        results = []
        for article in self.composition:
            query = db.session.query(product_articles).filter_by(
                product_id=self.id, article_id=article.id).first()
            results.append({
                'article': article.to_dict(),
                'quantity_used': query.quantity_used if query else 1.0
            })
        return results
    
    def producible_quantity(self):
        if not self.composition:
            return 0
        
        max_possible = float('inf')
        for article in self.composition:
            query = db.session.query(product_articles).filter_by(
                product_id=self.id, article_id=article.id).first()
            qty_used = query.quantity_used if query else 1.0
            
            if qty_used > 0:
                possible = article.quantity // qty_used
                max_possible = min(max_possible, possible)
        
        return int(max_possible) if max_possible != float('inf') else 0
    
    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'price': self.price,
            'purchase_price': self.purchase_price or 0,
            'photo_url': self.photo_url,
            'composition': self.get_composition_details(),
            'producible_quantity': self.producible_quantity(),
            'user_id': self.user_id
        }