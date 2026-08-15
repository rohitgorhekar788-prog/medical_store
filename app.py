import os
import io
import csv
import base64
import re

from datetime import datetime, date, timedelta
from io import BytesIO

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    jsonify,
    send_file,
    Response
)

from flask_login import (
    LoginManager,
    login_user,
    logout_user,
    login_required,
    current_user
)

from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash

from sqlalchemy import or_

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from models import (
    db,
    User,
    Medicine,
    Supplier,
    Sale,
    SaleItem,
    Batch,
    PurchaseOrder,
    Expense,
    CashDrawer
)

from ai_engine import AIEngine


# =========================================================
# STOCK / MEDICINE HELPERS
# =========================================================

def normalize_medicine_name(name):
    """Return a stable normalized medicine name for duplicate prevention."""
    return re.sub(r"\s+", " ", (name or "").strip()).casefold()


def get_batch_stock(medicine_id):
    """Return total positive stock stored in batches for a medicine."""
    total = (
        db.session.query(db.func.coalesce(db.func.sum(Batch.quantity), 0))
        .filter(Batch.medicine_id == medicine_id, Batch.quantity > 0)
        .scalar()
    )
    return int(total or 0)


def sync_medicine_stock(med):
    """Keep Medicine.current_stock synchronized with its batches when batches exist."""
    batch_rows = Batch.query.filter_by(medicine_id=med.id).all()
    if batch_rows:
        med.current_stock = sum(max(0, int(b.quantity or 0)) for b in batch_rows)
    else:
        med.current_stock = max(0, int(med.current_stock or 0))
    return med.current_stock


def deduct_from_batches(med, quantity):
    """Deduct quantity FIFO-style from batches. Returns False if batch stock is insufficient."""
    batches = (
        Batch.query
        .filter_by(medicine_id=med.id)
        .filter(Batch.quantity > 0)
        .order_by(Batch.expiry_date.asc(), Batch.id.asc())
        .all()
    )

    batch_total = sum(int(b.quantity or 0) for b in batches)
    if batch_total < quantity:
        return False, batch_total

    remaining = quantity
    for batch in batches:
        if remaining <= 0:
            break
        take = min(int(batch.quantity or 0), remaining)
        batch.quantity -= take
        remaining -= take

    sync_medicine_stock(med)
    return True, med.current_stock


def merge_duplicate_medicines():
    """One-time safe cleanup: merge medicines whose names differ only by case/spacing."""
    medicines = Medicine.query.order_by(Medicine.id.asc()).all()
    groups = {}
    for med in medicines:
        key = normalize_medicine_name(med.name)
        if key:
            groups.setdefault(key, []).append(med)

    changed = False
    for meds in groups.values():
        if len(meds) < 2:
            continue

        # Prefer the record that already owns batches; otherwise the record with stock.
        canonical = max(
            meds,
            key=lambda m: (len(m.batches), int(m.current_stock or 0), -m.id)
        )

        for duplicate in meds:
            if duplicate.id == canonical.id:
                continue

            # Move batch records to canonical medicine.
            duplicate_batches = Batch.query.filter_by(medicine_id=duplicate.id).all()
            duplicate_batch_total = sum(max(0, int(b.quantity or 0)) for b in duplicate_batches)
            for batch in duplicate_batches:
                batch.medicine_id = canonical.id

            # If the duplicate had stock that was not represented by a Batch,
            # preserve that stock in a dedicated merged batch instead of losing it.
            unbatched_stock = max(0, int(duplicate.current_stock or 0) - duplicate_batch_total)
            if unbatched_stock > 0:
                db.session.add(Batch(
                    medicine_id=canonical.id,
                    batch_no=f"MERGED-{duplicate.id}-{datetime.now().strftime('%Y%m%d%H%M%S')}",
                    expiry_date=duplicate.expiry_date or canonical.expiry_date or date.today(),
                    quantity=unbatched_stock
                ))

            # Keep historical sales linked to the surviving medicine.
            for item in SaleItem.query.filter_by(medicine_id=duplicate.id).all():
                item.medicine_id = canonical.id

            canonical.current_stock = int(canonical.current_stock or 0) + int(duplicate.current_stock or 0)

            if not canonical.generic_name and duplicate.generic_name:
                canonical.generic_name = duplicate.generic_name
            if not canonical.category and duplicate.category:
                canonical.category = duplicate.category
            if not canonical.manufacturer and duplicate.manufacturer:
                canonical.manufacturer = duplicate.manufacturer
            if not canonical.batch_no and duplicate.batch_no:
                canonical.batch_no = duplicate.batch_no
            if not canonical.expiry_date and duplicate.expiry_date:
                canonical.expiry_date = duplicate.expiry_date
            if not canonical.image and getattr(duplicate, 'image', None):
                canonical.image = duplicate.image

            db.session.delete(duplicate)
            changed = True

        db.session.flush()
        sync_medicine_stock(canonical)

    if changed:
        db.session.commit()


# =========================================================
# APP CONFIGURATION
# =========================================================

app = Flask(__name__)

app.config['SECRET_KEY'] = 'pharma-inventory-super-secret-key-2026'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///medical_inventory.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

app.config['UPLOAD_FOLDER'] = os.path.join('static', 'uploads')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

db.init_app(app)


# =========================================================
# LOGIN MANAGER
# =========================================================

login_manager = LoginManager()
login_manager.login_view = 'login'
login_manager.init_app(app)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# =========================================================
# DATABASE INITIALIZATION
# =========================================================

with app.app_context():
    db.create_all()

    # Repair case/spacing duplicate medicine records once at startup.
    merge_duplicate_medicines()

    # Default Admin
    if not User.query.filter_by(username='admin').first():
        admin = User(
            username='admin',
            email='admin@pharma.com',
            role='Admin'
        )
        admin.set_password('admin123')
        db.session.add(admin)
        db.session.commit()

    # Default Supplier
    default_supplier = Supplier.query.first()
    if not default_supplier:
        default_supplier = Supplier(
            name="Apex Healthcare Distributors",
            contact_person="Sales Manager",
            phone="+91 9876543210",
            email="contact@apexpharma.com",
            address="Building 4, Industrial Zone, Pune"
        )
        db.session.add(default_supplier)
        db.session.commit()

    # Sample Medicine
    if not Medicine.query.first():
        sample_medicine = Medicine(
            name="Paracetamol 500mg",
            generic_name="Acetaminophen",
            category="Analgesics",
            manufacturer="Apex Laboratories",
            batch_no="BATCH-2026-A1",
            purchase_price=15.0,
            selling_price=25.0,
            current_stock=20,
            min_stock=5,
            expiry_date=date(2027, 12, 31),
            supplier_id=default_supplier.id
        )
        db.session.add(sample_medicine)
        db.session.commit()

        # Batch Entry for Sample Medicine
        sample_batch = Batch(
            medicine_id=sample_medicine.id,
            batch_no="BATCH-2026-A1",
            expiry_date=date(2027, 12, 31),
            quantity=20
        )
        db.session.add(sample_batch)
        db.session.commit()


# =========================================================
# AUTHENTICATION
# =========================================================

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password):
            login_user(user)
            flash('Successfully logged in!', 'success')
            return redirect(url_for('dashboard'))

        flash('Invalid username or password. Default Admin: admin / admin123', 'danger')

    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out successfully.', 'info')
    return redirect(url_for('login'))


# =========================================================
# DASHBOARD
# =========================================================

@app.route('/')
@app.route('/dashboard')
@login_required
def dashboard():
    search_query = (request.args.get('search') or '').strip()
    page = max(1, int(request.args.get('page', 1) or 1))
    per_page = 12

    query = Medicine.query
    if search_query:
        like = f"%{search_query}%"
        query = query.filter(
            or_(
                Medicine.name.ilike(like),
                Medicine.generic_name.ilike(like),
                Medicine.category.ilike(like),
                Medicine.manufacturer.ilike(like)
            )
        )

    pagination = query.order_by(Medicine.name.asc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    medicines = pagination.items

    for med in medicines:
        sync_medicine_stock(med)
    db.session.commit()

    total_meds = Medicine.query.count()
    total_stock_units = int(db.session.query(db.func.coalesce(db.func.sum(Medicine.current_stock), 0)).scalar() or 0)
    inv_value = float(db.session.query(
        db.func.coalesce(db.func.sum(Medicine.current_stock * Medicine.selling_price), 0)
    ).scalar() or 0.0)

    today = date.today()
    today_sales = float(db.session.query(db.func.coalesce(db.func.sum(Sale.total_amount), 0))
                        .filter(db.func.date(Sale.sale_date) == today).scalar() or 0.0)

    all_meds = Medicine.query.all()
    low_stock_list = [m for m in all_meds if m.stock_status in ['LOW STOCK', 'OUT OF STOCK']]
    expiring_list = [m for m in all_meds if m.expiry_date and m.expiry_date <= today + timedelta(days=90)]

    return render_template(
        'dashboard.html',
        medicines=medicines,
        total_count=total_meds,
        total_medicines=total_meds,
        total_stock_units=total_stock_units,
        total_inventory_value=inv_value,
        inv_value=inv_value,
        todays_sales=today_sales,
        today_sales=today_sales,
        total_alerts=len(low_stock_list) + len(expiring_list),
        low_stock_count=len(low_stock_list),
        expiring_count=len(expiring_list),
        search_query=search_query,
        page=page,
        total_pages=pagination.pages,
        pagination=pagination
    )


@app.route('/api/medicine/<int:med_id>')
@login_required
def medicine_api(med_id):
    med = Medicine.query.get_or_404(med_id)
    sync_medicine_stock(med)
    db.session.commit()
    return jsonify({
        'id': med.id,
        'name': med.name,
        'generic_name': med.generic_name or '',
        'category': med.category or '',
        'manufacturer': med.manufacturer or '',
        'price': round(float(med.selling_price or 0), 2),
        'purchase_price': round(float(med.purchase_price or 0), 2),
        'stock': int(med.current_stock or 0),
        'min_stock': int(med.min_stock or 0),
        'batch_no': med.batch_no or '',
        'expiry_date': med.expiry_date.strftime('%Y-%m-%d') if med.expiry_date else '',
        'composition': med.generic_name or '',
        'stock_status': med.stock_status
    })


# =========================================================
# AI VISION SCANNER & API
# =========================================================

@app.route('/scanner', methods=['GET', 'POST'])
@login_required
def scanner():
    if request.method == 'POST':
        data = request.get_json() or {}
        image_data = data.get('image', '')

        if not image_data:
            return jsonify({'success': False, 'message': 'No image data received.'})

        try:
            header, encoded = (
                image_data.split(',', 1) if ',' in image_data else ('', image_data)
            )
            image_bytes = base64.b64decode(encoded)

            temp_path = os.path.join(
                app.config['UPLOAD_FOLDER'], f"temp_scan_{current_user.id}.jpg"
            )

            with open(temp_path, 'wb') as f:
                f.write(image_bytes)

            all_medicines = Medicine.query.all()
            matched_med, confidence, ocr_text = AIEngine.recognize_medicine_from_image(
                temp_path, all_medicines
            )

            if os.path.exists(temp_path):
                os.remove(temp_path)

            if matched_med:
                sync_medicine_stock(matched_med)
                db.session.commit()
                return jsonify({
                    'success': True,
                    'id': matched_med.id,
                    'name': matched_med.name,
                    'generic_name': matched_med.generic_name or 'N/A',
                    'category': matched_med.category or 'General',
                    'manufacturer': matched_med.manufacturer or 'N/A',
                    'price': float(matched_med.selling_price),
                    'stock': matched_med.current_stock,
                    'batch_no': getattr(matched_med, 'batch_no', f'BT-{matched_med.id}'),
                    'expiry_date': matched_med.expiry_date.strftime('%Y-%m-%d') if matched_med.expiry_date else 'N/A',
                    'confidence': confidence
                })

            return jsonify({'success': False, 'message': 'Medicine image not recognized in database.'})

        except Exception as e:
            return jsonify({'success': False, 'message': f'Processing Error: {str(e)}'})

    return render_template('scanner.html')


@app.route('/api/deduct-stock', methods=['POST'])
@login_required
def deduct_stock():
    try:
        data = request.get_json(silent=True) or {}
        med_id = data.get('medicine_id')
        raw_qty = data.get('quantity', 1)

        qty_matches = re.findall(r'\d+', str(raw_qty))
        qty_to_deduct = int(qty_matches[0]) if qty_matches else 0

        if not med_id:
            return jsonify({'success': False, 'message': 'Invalid Medicine ID.'}), 400
        if qty_to_deduct <= 0:
            return jsonify({'success': False, 'message': 'Quantity must be greater than 0.'}), 400

        med = Medicine.query.get(med_id)
        if not med:
            return jsonify({'success': False, 'message': 'Medicine not found.'}), 404

        # If batches exist, they are the source of truth. Otherwise use current_stock.
        batches_exist = Batch.query.filter_by(medicine_id=med.id).count() > 0

        if batches_exist:
            sync_medicine_stock(med)
            if med.current_stock < qty_to_deduct:
                return jsonify({
                    'success': False,
                    'message': f'Insufficient stock! Available: {med.current_stock} units.'
                }), 400

            ok, remaining = deduct_from_batches(med, qty_to_deduct)
            if not ok:
                return jsonify({
                    'success': False,
                    'message': f'Insufficient batch stock! Available: {remaining} units.'
                }), 400
        else:
            if int(med.current_stock or 0) < qty_to_deduct:
                return jsonify({
                    'success': False,
                    'message': f'Insufficient stock! Available: {med.current_stock or 0} units.'
                }), 400
            med.current_stock = int(med.current_stock or 0) - qty_to_deduct

        sale = Sale(
            total_amount=(med.selling_price * qty_to_deduct),
            payment_mode='Cash'
        )
        db.session.add(sale)
        db.session.flush()

        sale_item = SaleItem(
            sale_id=sale.id,
            medicine_id=med.id,
            quantity=qty_to_deduct,
            unit_price=med.selling_price
        )
        db.session.add(sale_item)

        db.session.commit()

        return jsonify({
            'success': True,
            'medicine_id': med.id,
            'new_stock': int(med.current_stock or 0),
            'message': f'Stock updated successfully. Remaining stock: {med.current_stock} units.'
        }), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({
            'success': False,
            'message': f'Database Error: {str(e)}'
        }), 500


# =========================================================
# POINT OF SALE / BILLING (Updated with Batch Deduction)
# =========================================================

@app.route('/pos', methods=['GET', 'POST'])
@login_required
def pos():
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        items = data.get('items', [])
        payment_mode = data.get('payment_mode', 'Cash')

        if not items:
            return jsonify({'success': False, 'message': 'Shopping cart is empty!'})

        try:
            total = 0.0
            sale = Sale(total_amount=0.0, payment_mode=payment_mode)
            db.session.add(sale)
            db.session.flush()

            for item in items:
                med = Medicine.query.get(item.get('id'))
                if not med:
                    raise ValueError('Medicine not found.')

                raw_item_qty = item.get('qty', 1)
                item_qty_matches = re.findall(r'\d+', str(raw_item_qty))
                qty_to_deduct = int(item_qty_matches[0]) if item_qty_matches else 0
                if qty_to_deduct <= 0:
                    raise ValueError(f'Invalid quantity for {med.name}.')

                batches_exist = Batch.query.filter_by(medicine_id=med.id).count() > 0
                if batches_exist:
                    sync_medicine_stock(med)
                    if med.current_stock < qty_to_deduct:
                        raise ValueError(f'Insufficient stock for {med.name}. Available: {med.current_stock} units.')
                    ok, remaining = deduct_from_batches(med, qty_to_deduct)
                    if not ok:
                        raise ValueError(f'Insufficient batch stock for {med.name}. Available: {remaining} units.')
                else:
                    if int(med.current_stock or 0) < qty_to_deduct:
                        raise ValueError(f'Insufficient stock for {med.name}. Available: {med.current_stock or 0} units.')
                    med.current_stock = int(med.current_stock or 0) - qty_to_deduct

                item_total = med.selling_price * qty_to_deduct
                total += item_total

                db.session.add(SaleItem(
                    sale_id=sale.id,
                    medicine_id=med.id,
                    quantity=qty_to_deduct,
                    unit_price=med.selling_price
                ))

            sale.total_amount = total
            db.session.commit()
            return jsonify({'success': True, 'sale_id': sale.id})

        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'message': str(e)})

    meds = Medicine.query.filter(Medicine.current_stock > 0).all()
    for med in meds:
        if Batch.query.filter_by(medicine_id=med.id).count() > 0:
            sync_medicine_stock(med)
    db.session.commit()
    meds = Medicine.query.filter(Medicine.current_stock > 0).all()
    return render_template('pos.html', medicines=meds)


# =========================================================
# PDF INVOICE
# =========================================================

@app.route('/download_invoice/<int:sale_id>')
@login_required
def download_invoice(sale_id):
    sale = Sale.query.get_or_404(sale_id)
    buffer = BytesIO()

    p = canvas.Canvas(buffer, pagesize=letter)
    p.setFont("Helvetica-Bold", 18)
    p.drawString(50, 750, "PharmaAI Medical Store")

    p.setFont("Helvetica", 10)
    p.drawString(50, 735, "123 Healthcare Avenue, Tech City")
    p.drawString(50, 722, "Contact: +91 98765 43210")

    p.setFont("Helvetica-Bold", 12)
    p.drawString(400, 750, "TAX INVOICE")

    p.setFont("Helvetica", 10)
    p.drawString(400, 735, f"Invoice No: #INV-{sale.id:05d}")
    p.drawString(400, 722, f"Date: {sale.sale_date.strftime('%Y-%m-%d %H:%M') if sale.sale_date else ''}")
    p.drawString(400, 709, f"Payment: {sale.payment_mode}")

    p.line(50, 695, 550, 695)

    p.setFont("Helvetica-Bold", 10)
    p.drawString(50, 680, "Item Description")
    p.drawString(280, 680, "Qty")
    p.drawString(350, 680, "Unit Price")
    p.drawString(470, 680, "Amount")

    p.line(50, 672, 550, 672)

    y = 655
    p.setFont("Helvetica", 10)

    for item in sale.items:
        p.drawString(50, y, f"{item.medicine.name}")
        p.drawString(280, y, f"{item.quantity}")
        p.drawString(350, y, f"₹{item.unit_price:.2f}")
        p.drawString(470, y, f"₹{(item.quantity * item.unit_price):.2f}")
        y -= 20

    p.line(50, y + 10, 550, y + 10)
    p.setFont("Helvetica-Bold", 11)
    p.drawString(350, y - 10, "Grand Total:")
    p.drawString(470, y - 10, f"₹{sale.total_amount:.2f}")

    p.setFont("Helvetica-Oblique", 9)
    p.drawString(180, y - 50, "Thank you for choosing PharmaAI Medical Store!")

    p.showPage()
    p.save()

    buffer.seek(0)
    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"Invoice_INV_{sale.id}.pdf",
        mimetype='application/pdf'
    )


# =========================================================
# AI ASSISTANT
# =========================================================

@app.route('/assistant', methods=['GET', 'POST'])
@login_required
def assistant():
    if request.method == 'GET':
        return render_template('assistant.html')

    data = request.get_json(silent=True) or request.form.to_dict()
    user_prompt = (data.get('prompt') or data.get('message') or '').strip()
    if not user_prompt:
        return jsonify({'reply': 'Please type a question. You can ask about medicines, stock, sales, profit, expenses, expiry or pharmacy analysis.'})

    all_meds = Medicine.query.order_by(Medicine.name.asc()).all()
    today = date.today()
    now = datetime.now()
    month_start = today.replace(day=1)
    year_start = today.replace(month=1, day=1)

    def sales_between(start_date):
        rows = Sale.query.filter(Sale.sale_date >= datetime.combine(start_date, datetime.min.time())).all()
        sales = sum(float(s.total_amount or 0) for s in rows)
        units = sum(int(i.quantity or 0) for s in rows for i in s.items)
        cost = sum(float(i.medicine.purchase_price or 0) * int(i.quantity or 0) for s in rows for i in s.items if i.medicine)
        return rows, sales, units, cost, sales - cost

    today_rows, today_sales, today_units, today_cost, today_profit = sales_between(today)
    month_rows, month_sales, month_units, month_cost, month_profit = sales_between(month_start)
    year_rows, year_sales, year_units, year_cost, year_profit = sales_between(year_start)
    total_sales = float(db.session.query(db.func.coalesce(db.func.sum(Sale.total_amount), 0)).scalar() or 0)
    total_expenses = float(db.session.query(db.func.coalesce(db.func.sum(Expense.amount), 0)).scalar() or 0)
    net_profit = year_profit - sum(float(e.amount or 0) for e in Expense.query.filter(Expense.expense_date >= datetime.combine(year_start, datetime.min.time())).all())

    low_stock = [m for m in all_meds if m.stock_status == 'LOW STOCK']
    out_stock = [m for m in all_meds if m.stock_status == 'OUT OF STOCK']
    expired = [m for m in all_meds if m.expiry_date and m.expiry_date < today]
    expiring = [m for m in all_meds if m.expiry_date and today <= m.expiry_date <= today + timedelta(days=90)]

    top_map = {}
    profit_map = {}
    for sale in Sale.query.all():
        for item in sale.items:
            if not item.medicine:
                continue
            name = item.medicine.name
            qty = int(item.quantity or 0)
            top_map[name] = top_map.get(name, 0) + qty
            profit_map[name] = profit_map.get(name, 0) + (float(item.unit_price or 0) - float(item.medicine.purchase_price or 0)) * qty
    top_sellers = sorted(top_map.items(), key=lambda x: x[1], reverse=True)[:5]
    top_profit = sorted(profit_map.items(), key=lambda x: x[1], reverse=True)[:5]

    context = {
        'medicines': all_meds,
        'total_meds': len(all_meds),
        'total_sales': total_sales,
        'today_sales': today_sales,
        'today_units': today_units,
        'today_cost': today_cost,
        'today_profit': today_profit,
        'month_sales': month_sales,
        'month_units': month_units,
        'month_cost': month_cost,
        'month_profit': month_profit,
        'year_sales': year_sales,
        'year_units': year_units,
        'year_cost': year_cost,
        'year_profit': year_profit,
        'total_expenses': total_expenses,
        'net_profit': net_profit,
        'low_stock': low_stock,
        'out_stock': out_stock,
        'expired': expired,
        'expiring': expiring,
        'top_sellers': top_sellers,
        'top_profit': top_profit,
    }
    reply = AIEngine.query_assistant(user_prompt, context)
    return jsonify({'reply': reply})


# =========================================================
# PRODUCTS PAGE (Updated with duplicate prevention & stock sync)
# =========================================================

@app.route('/products', methods=['GET', 'POST'])
@login_required
def products():
    if request.method == 'POST':
        name = request.form.get('name')
        generic_name = request.form.get('generic_name')
        category = request.form.get('category')
        manufacturer = request.form.get('manufacturer')
        batch_no = request.form.get('batch_no')
        expiry_str = request.form.get('expiry_date')

        if not name:
            flash('Medicine Name is required!', 'danger')
            return redirect(url_for('products'))

        expiry_date = None
        if expiry_str:
            try:
                expiry_date = datetime.strptime(expiry_str.strip(), '%Y-%m-%d').date()
            except ValueError:
                try:
                    expiry_date = datetime.strptime(expiry_str.strip(), '%d-%m-%Y').date()
                except ValueError:
                    flash('Invalid date format for Expiry Date. Use YYYY-MM-DD.', 'danger')
                    return redirect(url_for('products'))

        try:
            selling_price = float(request.form.get('selling_price', 0.0) or 0.0)
            purchase_price = float(request.form.get('purchase_price', request.form.get('unit_price', 0.0)) or 0.0)
            
            # Extract numbers from stock value safely using regex to handle text suffixes like "units"
            raw_stock = str(request.form.get('current_stock', 0))
            stock_numbers = re.findall(r'\d+', raw_stock)
            current_stock = int(stock_numbers[0]) if stock_numbers else 0

            min_stock = int(request.form.get('min_stock', 5) or 5)
        except (ValueError, TypeError, IndexError):
            flash('Please enter valid numeric values for price and stock.', 'danger')
            return redirect(url_for('products'))

        default_sup = Supplier.query.first()
        supplier_id = default_sup.id if default_sup else 1

        file = request.files.get('image')
        filename = None
        if file and file.filename != '':
            filename = secure_filename(file.filename)
            upload_path = app.config.get('UPLOAD_FOLDER', 'static/uploads')
            os.makedirs(upload_path, exist_ok=True)
            file.save(os.path.join(upload_path, filename))

        try:
            existing_med = Medicine.query.filter(db.func.lower(Medicine.name) == normalize_medicine_name(name)).first()
            if existing_med:
                existing_med.current_stock += current_stock
                if batch_no:
                    existing_med.batch_no = batch_no
                if expiry_date:
                    existing_med.expiry_date = expiry_date
                
                if current_stock > 0:
                    new_batch = Batch(
                        medicine_id=existing_med.id,
                        batch_no=batch_no or f"BATCH-{datetime.now().strftime('%Y%m%d%H%M%S')}",
                        expiry_date=expiry_date,
                        quantity=current_stock
                    )
                    db.session.add(new_batch)
                
                db.session.commit()
                flash('Existing medicine stock updated successfully!', 'success')
                return redirect(url_for('products'))

            new_med = Medicine(
                name=name,
                generic_name=generic_name,
                category=category,
                manufacturer=manufacturer,
                batch_no=batch_no or f"BATCH-{datetime.now().strftime('%Y%m%d%H%M%S')}",
                expiry_date=expiry_date,
                purchase_price=purchase_price,
                selling_price=selling_price,
                current_stock=current_stock,
                min_stock=min_stock,
                supplier_id=supplier_id
            )
            
            if hasattr(Medicine, 'image') and filename:
                new_med.image = filename

            db.session.add(new_med)
            db.session.flush()

            if current_stock > 0:
                new_batch = Batch(
                    medicine_id=new_med.id,
                    batch_no=new_med.batch_no,
                    expiry_date=expiry_date,
                    quantity=current_stock
                )
                db.session.add(new_batch)

            db.session.commit()
            flash('New Medicine and stock batch added successfully!', 'success')
            return redirect(url_for('products'))

        except Exception as e:
            db.session.rollback()
            flash(f'Error adding product: {str(e)}', 'danger')
            return redirect(url_for('products'))

    all_medicines = Medicine.query.order_by(Medicine.name).all()
    for med in all_medicines:
        if Batch.query.filter_by(medicine_id=med.id).count() > 0:
            sync_medicine_stock(med)
    db.session.commit()
    return render_template('products.html', medicines=all_medicines)


# =========================================================
# STOCK / INVENTORY PAGE
# =========================================================

@app.route('/inventory', methods=['GET', 'POST'])
@login_required
def inventory():
    if request.method == 'POST':
        try:
            medicine_id = int(request.form.get('medicine_id'))
            batch_no = request.form.get('batch_no')
            expiry_str = request.form.get('expiry_date')
            
            # Robust parsing for inventory stock input (handles text/units properly)
            raw_qty = request.form.get('quantity', 0)
            qty_matches = re.findall(r'\d+', str(raw_qty))
            quantity = int(qty_matches[0]) if qty_matches else 0

            expiry_date = (
                datetime.strptime(expiry_str, '%Y-%m-%d').date()
                if expiry_str else None
            )
        except (ValueError, TypeError, IndexError):
            flash('Please enter valid stock details.', 'danger')
            return redirect(url_for('inventory'))

        med = Medicine.query.get(medicine_id)
        if not med:
            flash('Medicine not found.', 'danger')
            return redirect(url_for('inventory'))

        try:
            new_batch = Batch(
                medicine_id=medicine_id,
                batch_no=batch_no,
                expiry_date=expiry_date,
                quantity=quantity
            )
            db.session.add(new_batch)

            med.current_stock = (med.current_stock or 0) + quantity
            if batch_no:
                med.batch_no = batch_no
            if expiry_date:
                med.expiry_date = expiry_date

            db.session.commit()
            flash('Stock batch added successfully and total stock updated!', 'success')
            return redirect(url_for('inventory'))

        except Exception as e:
            db.session.rollback()
            flash(f'Error adding stock batch: {str(e)}', 'danger')
            return redirect(url_for('inventory'))

    all_batches = Batch.query.join(Medicine).order_by(Batch.expiry_date.asc()).all()
    all_medicines = Medicine.query.order_by(Medicine.name).all()
    return render_template('inventory.html', batches=all_batches, medicines=all_medicines)


# =========================================================
# DELETE MEDICINE
# =========================================================

@app.route('/delete-medicine/<int:med_id>', methods=['POST'])
@login_required
def delete_medicine(med_id):
    med = Medicine.query.get_or_404(med_id)
    medicine_name = med.name

    try:
        Batch.query.filter_by(medicine_id=med.id).delete()
        db.session.delete(med)
        db.session.commit()
        flash(f'{medicine_name} deleted successfully!', 'danger')
    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting medicine: {str(e)}', 'danger')

    return redirect(url_for('products'))


# =========================================================
# SALES HISTORY
# =========================================================

@app.route('/sales-history')
@login_required
def sales_history():
    all_sales = Sale.query.order_by(Sale.sale_date.desc()).all()
    return render_template('sales_history.html', sales=all_sales)


# =========================================================
# SALES & PROFIT
# =========================================================

@app.route('/sales-profit')
@login_required
def sales_profit():
    today = date.today()
    month_start = today.replace(day=1)
    year_start = today.replace(month=1, day=1)

    def period_stats(start_date, end_date=None):
        q = Sale.query.filter(Sale.sale_date >= datetime.combine(start_date, datetime.min.time()))
        if end_date:
            q = q.filter(Sale.sale_date < datetime.combine(end_date, datetime.min.time()))
        rows = q.order_by(Sale.sale_date.asc()).all()
        sales = sum(float(s.total_amount or 0) for s in rows)
        units = sum(int(i.quantity or 0) for s in rows for i in s.items)
        cost = sum(float(i.medicine.purchase_price or 0) * int(i.quantity or 0) for s in rows for i in s.items if i.medicine)
        return rows, sales, units, cost, sales - cost

    all_sales = Sale.query.order_by(Sale.sale_date.desc()).all()
    total_sales = sum(float(s.total_amount or 0) for s in all_sales)
    total_items_sold = sum(int(i.quantity or 0) for s in all_sales for i in s.items)
    total_cost = sum(float(i.medicine.purchase_price or 0) * int(i.quantity or 0) for s in all_sales for i in s.items if i.medicine)
    gross_profit = total_sales - total_cost
    total_expenses = float(db.session.query(db.func.coalesce(db.func.sum(Expense.amount), 0)).scalar() or 0)
    net_profit = gross_profit - total_expenses
    profit_margin = (gross_profit / total_sales * 100) if total_sales else 0

    _, today_sales, today_units, today_cost, today_profit = period_stats(today)
    _, month_sales, month_units, month_cost, month_profit = period_stats(month_start)
    _, year_sales, year_units, year_cost, year_profit = period_stats(year_start)

    last7_labels, last7_sales, last7_profit = [], [], []
    for offset in range(6, -1, -1):
        d = today - timedelta(days=offset)
        rows, sales, units, cost, profit = period_stats(d, d + timedelta(days=1))
        last7_labels.append(d.strftime('%d %b'))
        last7_sales.append(round(sales, 2))
        last7_profit.append(round(profit, 2))

    last12_labels, last12_sales, last12_profit = [], [], []
    for offset in range(11, -1, -1):
        month_index = today.year * 12 + (today.month - 1) - offset
        y = month_index // 12
        m = month_index % 12 + 1
        first = date(y, m, 1)
        next_month = date(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1)
        rows, sales, units, cost, profit = period_stats(first, next_month)
        last12_labels.append(first.strftime('%b %Y'))
        last12_sales.append(round(sales, 2))
        last12_profit.append(round(profit, 2))

    top_map = {}
    for sale in all_sales:
        for item in sale.items:
            if item.medicine:
                top_map[item.medicine.name] = top_map.get(item.medicine.name, 0) + int(item.quantity or 0)
    top_rows = sorted(top_map.items(), key=lambda x: x[1], reverse=True)[:8]

    recent_sales = all_sales[:10]
    return render_template(
        'sales_profit.html',
        total_sales=total_sales, total_cost=total_cost, gross_profit=gross_profit,
        net_profit=net_profit, total_orders=len(all_sales), total_items_sold=total_items_sold,
        profit_margin=profit_margin, total_expenses=total_expenses,
        today_units=today_units, today_sales=today_sales, today_cost=today_cost, today_profit=today_profit,
        month_units=month_units, month_sales=month_sales, month_cost=month_cost, month_profit=month_profit,
        year_units=year_units, year_sales=year_sales, year_cost=year_cost, year_profit=year_profit,
        last_7_days_labels=last7_labels, last_7_days_sales=last7_sales, last_7_days_profit=last7_profit,
        last_12_months_labels=last12_labels, last_12_months_sales=last12_sales, last_12_months_profit=last12_profit,
        top_medicine_labels=[x[0] for x in top_rows], top_medicine_units=[x[1] for x in top_rows],
        recent_sales=recent_sales
    )


# =========================================================
# ALERTS
# =========================================================

@app.route('/alerts')
@login_required
def alerts():
    today = date.today()
    thirty_days_later = today + timedelta(days=30)

    low_stock_meds = Medicine.query.filter(
        Medicine.current_stock <= Medicine.min_stock
    ).all()

    out_of_stock_meds = Medicine.query.filter(
        Medicine.current_stock == 0
    ).all()

    expiring_batches = Batch.query.filter(
        Batch.expiry_date <= thirty_days_later,
        Batch.quantity > 0
    ).all()

    return render_template(
        'alerts.html',
        low_stock=low_stock_meds,
        out_of_stock=out_of_stock_meds,
        expiring=expiring_batches
    )


# =========================================================
# SUPPLIERS
# =========================================================

@app.route('/suppliers', methods=['GET', 'POST'])
@login_required
def suppliers():
    if request.method == 'POST':
        name = request.form.get('name')
        contact_person = request.form.get('contact_person')
        phone = request.form.get('phone')
        email = request.form.get('email')
        address = request.form.get('address')

        new_supplier = Supplier(
            name=name,
            contact_person=contact_person,
            phone=phone,
            email=email,
            address=address
        )
        db.session.add(new_supplier)
        db.session.commit()

        flash('Supplier added successfully!', 'success')
        return redirect(url_for('suppliers'))

    all_suppliers = Supplier.query.all()
    return render_template('suppliers.html', suppliers=all_suppliers)


# =========================================================
# PURCHASE ORDERS
# =========================================================

@app.route('/purchase-orders', methods=['GET', 'POST'])
@login_required
def purchase_orders():
    if request.method == 'POST':
        try:
            supplier_id = int(request.form.get('supplier_id'))
            total_cost = float(request.form.get('total_cost', 0))
        except (ValueError, TypeError):
            flash('Please enter valid purchase order details.', 'danger')
            return redirect(url_for('purchase_orders'))

        po = PurchaseOrder(
            supplier_id=supplier_id,
            total_cost=total_cost,
            status='Pending'
        )
        db.session.add(po)
        db.session.commit()

        flash('Purchase Order created successfully!', 'success')
        return redirect(url_for('purchase_orders'))

    orders = PurchaseOrder.query.order_by(PurchaseOrder.order_date.desc()).all()
    suppliers_list = Supplier.query.all()

    return render_template('purchase_orders.html', orders=orders, suppliers=suppliers_list)


# =========================================================
# EXPENSES
# =========================================================

@app.route('/expenses', methods=['GET', 'POST'])
@login_required
def expenses():
    if request.method == 'POST':
        title = request.form.get('title')
        category = request.form.get('category')
        notes = request.form.get('notes')

        try:
            amount = float(request.form.get('amount', 0))
        except ValueError:
            flash('Please enter a valid amount.', 'danger')
            return redirect(url_for('expenses'))

        exp = Expense(
            title=title,
            category=category,
            amount=amount,
            notes=notes
        )
        db.session.add(exp)
        db.session.commit()

        flash('Expense recorded successfully!', 'success')
        return redirect(url_for('expenses'))

    all_expenses = Expense.query.order_by(Expense.expense_date.desc()).all()
    total_expense = sum(e.amount for e in all_expenses)

    return render_template('expenses.html', expenses=all_expenses, total_expense=total_expense)


# =========================================================
# CASH DRAWER
# =========================================================

@app.route('/cash-drawer', methods=['GET', 'POST'])
@login_required
def cash_drawer():
    today = date.today()
    record = CashDrawer.query.filter_by(date=today).first()

    if request.method == 'POST':
        try:
            opening = float(request.form.get('opening_cash', 0))
            closing = float(request.form.get('closing_cash', 0))
        except ValueError:
            flash('Please enter valid cash amounts.', 'danger')
            return redirect(url_for('cash_drawer'))

        notes = request.form.get('notes')

        if not record:
            record = CashDrawer(
                date=today,
                opening_cash=opening,
                closing_cash=closing,
                notes=notes
            )
            db.session.add(record)
        else:
            record.opening_cash = opening
            record.closing_cash = closing
            record.notes = notes

        db.session.commit()
        flash('Cash drawer updated for today!', 'success')
        return redirect(url_for('cash_drawer'))

    today_cash_sales = (
        db.session.query(db.func.sum(Sale.total_amount))
        .filter(
            db.func.date(Sale.sale_date) == today,
            Sale.payment_mode == 'Cash'
        ).scalar() or 0.0
    )

    return render_template('cash_drawer.html', record=record, today_cash_sales=today_cash_sales)


# =========================================================
# AI DEMAND FORECAST
# =========================================================

@app.route('/forecast')
@login_required
def forecast():
    medicines = Medicine.query.all()
    forecast_data = []

    for med in medicines:
        predicted_demand = int(med.min_stock * 1.5)
        reorder_status = "High Priority" if med.current_stock < med.min_stock else "Sufficient"

        forecast_data.append({
            'medicine': med,
            'predicted_demand': predicted_demand,
            'suggested_reorder': max(0, predicted_demand - med.current_stock),
            'status': reorder_status
        })

    return render_template('forecast.html', forecast_data=forecast_data)


# =========================================================
# AI ANALYTICS
# =========================================================

@app.route('/analytics')
@login_required
def analytics():
    total_sales = db.session.query(db.func.sum(Sale.total_amount)).scalar() or 0.0
    total_orders = Sale.query.count()
    medicines_count = Medicine.query.count()

    fast_moving = Medicine.query.order_by(Medicine.current_stock.desc()).limit(5).all()

    return render_template(
        'analytics.html',
        total_sales=total_sales,
        total_orders=total_orders,
        medicines_count=medicines_count,
        fast_moving=fast_moving
    )


# =========================================================
# USERS MANAGEMENT
# =========================================================

@app.route('/users', methods=['GET', 'POST'])
@login_required
def users_page():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        role = request.form.get('role', 'Staff')

        existing_user = User.query.filter_by(username=username).first()

        if existing_user:
            flash('Username already exists!', 'danger')
        else:
            new_user = User(username=username, role=role)
            new_user.set_password(password)
            db.session.add(new_user)
            db.session.commit()
            flash('New user created successfully!', 'success')

        return redirect(url_for('users_page'))

    all_users = User.query.all()
    return render_template('users.html', users=all_users)


# =========================================================
# MY PROFILE
# =========================================================

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        new_password = request.form.get('password')
        if new_password:
            current_user.set_password(new_password)
            db.session.commit()
            flash('Password updated successfully!', 'success')
        return redirect(url_for('profile'))

    return render_template('profile.html')


# =========================================================
# MAIN ENTRY POINT
# =========================================================

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)