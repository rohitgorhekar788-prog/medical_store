import re
from datetime import date, timedelta


class AIEngine:
    """Rule-based pharmacy copilot that uses live database context."""

    @staticmethod
    def _norm(text):
        return re.sub(r"\s+", " ", (text or "").strip()).casefold()

    @staticmethod
    def _money(v):
        return f"₹{float(v or 0):,.2f}"

    @staticmethod
    def recognize_medicine_from_image(image_path, all_medicines):
        if not all_medicines:
            return None, 0, "No medicines in database"
        extracted_text = ""
        try:
            import shutil
            import pytesseract
            from PIL import Image
            detected = shutil.which('tesseract')
            if detected:
                pytesseract.pytesseract.tesseract_cmd = detected
            img = Image.open(image_path)
            extracted_text = pytesseract.image_to_string(img)
        except Exception as e:
            print(f"OCR error: {e}")
        text = AIEngine._norm(extracted_text)
        if not text:
            return None, 0, "No text detected"
        candidates = []
        for med in all_medicines:
            name = AIEngine._norm(med.name)
            generic = AIEngine._norm(med.generic_name)
            score = 0
            if name and name in text:
                score = 100
            else:
                words = [w for w in re.findall(r"[a-z0-9]+", name) if len(w) > 2]
                if words:
                    score = max(score, int(70 + 25 * sum(w in text for w in words) / len(words)))
            if generic and generic in text:
                score = max(score, 90)
            if score:
                candidates.append((score, int(med.current_stock or 0), med))
        if candidates:
            candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
            best = candidates[0]
            return best[2], best[0], extracted_text
        return None, 0, extracted_text

    @staticmethod
    def _find_medicine(prompt, medicines):
        p = AIEngine._norm(prompt)
        best = None
        best_score = 0
        for med in medicines:
            fields = [med.name, med.generic_name, med.category, med.manufacturer]
            score = 0
            for field in fields:
                f = AIEngine._norm(field)
                if not f:
                    continue
                if f in p:
                    score = max(score, 100 if field == med.name else 90)
                else:
                    words = [w for w in re.findall(r"[a-z0-9]+", f) if len(w) > 2]
                    matched = sum(w in p for w in words)
                    if words and matched:
                        score = max(score, int(55 + 40 * matched / len(words)))
            if score > best_score:
                best_score, best = score, med
        return best

    @staticmethod
    def query_assistant(prompt, db_context):
        p = AIEngine._norm(prompt)
        meds = db_context.get('medicines', [])
        med = AIEngine._find_medicine(prompt, meds)

        # Medicine-specific questions
        medicine_words = ['medicine', 'tablet', 'capsule', 'drug', 'medication', 'salt', 'composition', 'use', 'uses', 'purpose', 'dose', 'dosage', 'price', 'manufacturer', 'expiry', 'batch', 'औषध', 'गोळी', 'मेडिसिन', 'कशासाठी', 'उपयोग', 'किंमत']
        if med and (any(w in p for w in medicine_words) or len(p.split()) <= 5):
            stock = int(med.current_stock or 0)
            status = med.stock_status
            answer = [
                f"💊 **{med.name}**",
                f"• Generic / Salt: {med.generic_name or 'Not recorded'}",
                f"• Category: {med.category or 'Not recorded'}",
                f"• Manufacturer: {med.manufacturer or 'Not recorded'}",
                f"• Selling price: {AIEngine._money(med.selling_price)}",
                f"• Purchase price: {AIEngine._money(med.purchase_price)}",
                f"• Current stock: {stock} units ({status})",
                f"• Batch: {med.batch_no or 'Not recorded'}",
                f"• Expiry: {med.expiry_date.strftime('%d-%m-%Y') if med.expiry_date else 'Not recorded'}"
            ]
            return "\n".join(answer) + "\n\nFor medical use, follow the product label or advice of a qualified healthcare professional; the inventory system should not be used to decide a patient's dose."

        # Stock / inventory
        if any(w in p for w in ['low stock', 'low-stock', 'running low', ' कमी', 'कमी', 'stock कमी', 'out of stock', 'empty stock', 'inventory', 'stock']):
            low = db_context.get('low_stock', [])
            out = db_context.get('out_stock', [])
            lines = [f"📦 **Inventory status:** {db_context.get('total_meds', 0)} medicine types are in the database.", f"• Low stock: {len(low)}", f"• Out of stock: {len(out)}"]
            if out:
                lines.append("\n🔴 Out of stock: " + ", ".join(m.name for m in out[:10]))
            if low:
                lines.append("🟠 Low stock: " + ", ".join(f"{m.name} ({m.current_stock})" for m in low[:10]))
            return "\n".join(lines)

        # Expiry
        if any(w in p for w in ['expiry', 'expire', 'expired', 'expiring', 'expiry soon', 'मुदत', 'एक्सपायरी']):
            expired = db_context.get('expired', [])
            expiring = db_context.get('expiring', [])
            lines = [f"📅 Expiry analysis: {len(expired)} expired and {len(expiring)} expiring within 90 days."]
            if expired:
                lines.append("🔴 Expired: " + ", ".join(m.name for m in expired[:10]))
            if expiring:
                lines.append("🟠 Expiring soon: " + ", ".join(f"{m.name} ({m.expiry_date.strftime('%d-%m-%Y')})" for m in expiring[:10]))
            return "\n".join(lines)

        # Improvement ideas / recommendations
        # General pharmacy overview
        return (f"Hello! 👋 I am your PharmaAI Assistant. I can work with your live pharmacy data.\n\n"
                f"You currently have {db_context.get('total_meds', 0)} medicine types. Today's sales are {AIEngine._money(db_context.get('today_sales', 0))}.\n\n"
                "Try asking:\n• 'Tell me about Paracetamol'\n• 'Which medicines are low in stock?'\n• 'Show today's sales and profit'\n• 'Which medicines are expiring soon?'\n• 'Give me pharmacy analysis and ideas'")
