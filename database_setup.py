import sqlite3
import pandas as pd
import requests
import io

DB_NAME = "pharma.db"

# Correct Raw CSV URL from junioralive/Indian-Medicine-Dataset GitHub Repository
DATASET_URL = "https://raw.githubusercontent.com/junioralive/Indian-Medicine-Dataset/main/DATA/indian_medicine_data.csv"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # 1. Medicines Table तयार करणे
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS medicines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price REAL,
            manufacturer TEXT,
            pack_size TEXT,
            composition TEXT,
            salt_composition TEXT
        )
    ''')

    # 2. Users Table (लॉगिन सिस्टीमसाठी)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    ''')

    # डिफॉल्ट ॲडमिन युझर ॲड करा
    cursor.execute('''
        INSERT OR IGNORE INTO users (username, password) 
        VALUES ('admin', 'admin123')
    ''')

    conn.commit()
    conn.close()
    print("✅ Database tables created successfully!")

def import_github_dataset():
    print("⏳ Downloading dataset from GitHub... Please wait.")
    try:
        response = requests.get(DATASET_URL)
        if response.status_code == 200:
            # CSV Load करणे
            csv_data = io.StringIO(response.text)
            df = pd.read_csv(csv_data)

            # CSV मधील कॉलम निवडून डेटाबेसच्या कॉलमशी जोडणे
            df_filtered = pd.DataFrame()
            df_filtered['name'] = df.get('name', '')
            df_filtered['price'] = df.get('price', 0)
            df_filtered['manufacturer'] = df.get('manufacturer', '')
            df_filtered['pack_size'] = df.get('pack_size_label', '')
            df_filtered['composition'] = df.get('short_composition1', '')
            df_filtered['salt_composition'] = df.get('short_composition2', '')

            # Database मध्ये सेव्ह करणे
            conn = sqlite3.connect(DB_NAME)
            df_filtered.to_sql('medicines', conn, if_exists='append', index=False)
            conn.close()

            print(f"🎉 Successfully imported {len(df_filtered)} medicines into '{DB_NAME}' database!")
        else:
            print("❌ Failed to download CSV from GitHub. Status Code:", response.status_code)

    except Exception as e:
        print(f"❌ Error while importing dataset: {e}")

if __name__ == "__main__":
    init_db()
    import_github_dataset()