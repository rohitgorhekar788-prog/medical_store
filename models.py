from datetime import datetime, date
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

# 1. USER MODEL
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(50), default='Staff')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


# 2. SUPPLIER MODEL
class Supplier(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    contact_person = db.Column(db.String(100))
    phone = db.Column(db.String(20))
    email = db.Column(db.String(100))
    address = db.Column(db.Text)
    
    medicines = db.relationship('Medicine', backref='supplier', lazy=True)
    purchase_orders = db.relationship('PurchaseOrder', backref='supplier', lazy=True)


# 3. MEDICINE MODEL
class Medicine(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    generic_name = db.Column(db.String(150))
    category = db.Column(db.String(100))
    manufacturer = db.Column(db.String(150))
    batch_no = db.Column(db.String(50))
    purchase_price = db.Column(db.Float, default=0.0)
    selling_price = db.Column(db.Float, default=0.0)
    current_stock = db.Column(db.Integer, default=0)
    min_stock = db.Column(db.Integer, default=10)
    expiry_date = db.Column(db.Date, nullable=True)
    image = db.Column(db.String(255), nullable=True)  # Added Image Field
    
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'), nullable=True)
    batches = db.relationship('Batch', backref='medicine', lazy=True)
    sale_items = db.relationship('SaleItem', backref='medicine', lazy=True)

    @property
    def stock_status(self):
        if self.current_stock <= 0:
            return 'OUT OF STOCK'
        elif self.current_stock <= self.min_stock:
            return 'LOW STOCK'
        elif self.expiry_date and self.expiry_date <= date.today():
            return 'EXPIRED'
        return 'NORMAL'


# 4. BATCH MODEL
class Batch(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    medicine_id = db.Column(db.Integer, db.ForeignKey('medicine.id'), nullable=False)
    batch_no = db.Column(db.String(50), nullable=False)
    expiry_date = db.Column(db.Date, nullable=False)
    quantity = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# 5. SALE & SALE ITEM MODELS
class Sale(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sale_date = db.Column(db.DateTime, default=datetime.utcnow)
    total_amount = db.Column(db.Float, default=0.0)
    payment_mode = db.Column(db.String(50), default='Cash')
    
    items = db.relationship('SaleItem', backref='sale', lazy=True)


class SaleItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sale_id = db.Column(db.Integer, db.ForeignKey('sale.id'), nullable=False)
    medicine_id = db.Column(db.Integer, db.ForeignKey('medicine.id'), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Float, nullable=False)


# 6. PURCHASE ORDER MODEL
class PurchaseOrder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'), nullable=False)
    order_date = db.Column(db.DateTime, default=datetime.utcnow)
    total_cost = db.Column(db.Float, default=0.0)
    status = db.Column(db.String(50), default='Pending')


# 7. EXPENSE MODEL
class Expense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    category = db.Column(db.String(100))
    amount = db.Column(db.Float, default=0.0)
    expense_date = db.Column(db.DateTime, default=datetime.utcnow)
    notes = db.Column(db.Text)


# 8. CASH DRAWER MODEL
class CashDrawer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, default=date.today, unique=True)
    opening_cash = db.Column(db.Float, default=0.0)
    closing_cash = db.Column(db.Float, default=0.0)
    notes = db.Column(db.Text)