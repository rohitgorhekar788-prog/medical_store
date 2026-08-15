import random
from app import app, db, Medicine

categories = ['Tablets', 'Syrup', 'Injections', 'Ointment', 'Capsules', 'Drops', 'Gel']
manufacturers = ['Sun Pharma', 'Cipla', 'Mankind', 'Dr. Reddy', 'Torrent Pharma', 'Abbott', 'Alkem']

# रियलिस्टिक भारतीय औषधांची यादी
names = [
    'Amoxicillin', 'Azithromycin', 'Cetirizine', 'Dolo 650', 'Pantoprazole',
    'Metformin', 'Ibuprofen', 'Montelukast', 'Omeprazole', 'Amlodipine',
    'Paracetamol', 'Ciprofloxacin', 'Atorvastatin', 'Losartan', 'Ranitidine',
    'Telmisartan', 'Glimepiride', 'Voglibose', 'Clopidogrel', 'Rosuvastatin',
    'Levocetirizine', 'Combiflam', 'Limcee', 'Becosules', 'Evion 400',
    'Ascoril LS', 'Benadryl', 'Zincovit', 'Allegra', 'Meftal-Spas'
]

with app.app_context():
    db.create_all()
    
    added_count = 0
    for n in names:
        for dosage in [100, 250, 500]:  # प्रत्येक औषधाचे वेगवेगळे डोस
            med_name = f"{n} {dosage}mg"
            
            # आधीपासून औषध आहे का ते तपासा (Duplicate होणार नाही)
            existing = Medicine.query.filter_by(name=med_name).first()
            if not existing:
                med = Medicine(
                    name=med_name,
                    category=random.choice(categories),
                    current_stock=random.randint(5, 120),  # स्टॉक लेव्हल
                    selling_price=round(random.uniform(15, 350), 2),
                    purchase_price=round(random.uniform(5, 200), 2),
                    min_stock=10,
                    manufacturer=random.choice(manufacturers)
                )
                db.session.add(med)
                added_count += 1
            
    db.session.commit()
    print(f"Successfully added {added_count} new medicines to the database!")