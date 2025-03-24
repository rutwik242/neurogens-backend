from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from werkzeug.utils import secure_filename
from PIL import Image
from pymongo import MongoClient
from datetime import datetime
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
import google.generativeai as genai
import base64, io, os, requests
from bs4 import BeautifulSoup

# ==== Configuration ====
UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Gemini API
genai.configure(api_key=os.environ.get("GOOGLE_API_KEY"))
model = genai.GenerativeModel("gemini-1.5-flash")

# MongoDB Atlas
import os
client = MongoClient(os.environ.get("MONGODB_URI"))
db = client["product_catalog"]
collection = db["entries"]

# Flask app
app = Flask(__name__)
CORS(app)

# ==== Prompt ====
prompt = """
You are a product catalog generator. Given a product image, generate a structured catalog entry.

Format:
Product Name: <name>
Category: <category>
Description: <description>
Specifications:
- Feature 1: value
- Feature 2: value
...
"""

# ==== Helper: Web Scrape ====
def scrape_specs(product_name):
    headers = {'User-Agent': 'Mozilla/5.0'}
    query = f"{product_name} specifications"
    url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
    try:
        res = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(res.text, 'html.parser')
        snippets = soup.select("div.BNeawe.s3v9rd.AP7Wnd")
        info = [s.get_text(strip=True) for s in snippets if product_name.lower()[:5] in s.get_text(strip=True).lower()]
        return "\n".join(f"- {i}" for i in info[:5]) if info else "No detailed specs found online."
    except Exception as e:
        return f"Web scraping failed: {str(e)}"

# ==== Helper: Category Classifier ====
def classify_category(name, desc):
    text = f"{name} {desc}".lower()
    if any(k in text for k in ["laptop", "phone", "tablet", "tv", "headphones"]): return "Electronics"
    elif any(k in text for k in ["car", "bike", "scooter", "truck"]): return "Automobile"
    elif any(k in text for k in ["shirt", "jeans", "dress", "shoes", "sneakers"]): return "Fashion"
    elif any(k in text for k in ["bat", "ball", "cricket", "tennis", "football"]): return "Sports"
    elif any(k in text for k in ["sofa", "table", "chair", "bed"]): return "Furniture"
    elif any(k in text for k in ["fridge", "microwave", "washing machine"]): return "Appliances"
    elif any(k in text for k in ["book", "novel", "textbook"]): return "Books"
    elif any(k in text for k in ["makeup", "cream", "perfume"]): return "Beauty"
    elif any(k in text for k in ["toy", "doll", "lego"]): return "Toys"
    return "Other"

# ==== Route: Home ====
@app.route("/")
def home():
    return "✅ Neurogens Flask API running!"

# ==== Route: Generate Catalog ====
@app.route("/generate_catalog", methods=["POST"])
def generate_catalog():
    if "images" not in request.files:
        return jsonify({"error": "No images provided"}), 400

    files = request.files.getlist("images")
    results = []

    for file in files:
        try:
            filename = secure_filename(file.filename)
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            file.save(filepath)

            image = Image.open(filepath)
            response = model.generate_content([prompt, image])
            catalog_text = response.text.strip()

            # Extract product name
            product_name = "Unknown"
            lines = catalog_text.splitlines()
            for line in lines:
                if line.lower().startswith("product name"):
                    product_name = line.split(":", 1)[-1].strip()
                    break

            # Check if web scraping is needed
            specs_section = ""
            for i, line in enumerate(lines):
                if line.lower().startswith("specifications"):
                    specs_section = "\n".join(lines[i+1:]).strip()
                    break

            needs_scrape = (
                not specs_section or len(specs_section.splitlines()) < 5 or
                any("Requires further information" in line for line in specs_section.splitlines())
            )
            web_info = scrape_specs(product_name) if needs_scrape else ""

            # Classify category
            category = classify_category(product_name, catalog_text)

            # Convert image to base64
            with open(filepath, "rb") as f:
                image_base64 = base64.b64encode(f.read()).decode("utf-8")

            # Save to MongoDB
            record = {
                "filename": filename,
                "product_name": product_name,
                "category": category,
                "catalog_entry": catalog_text + ("\n\n🔎 Additional Info from Web:\n" + web_info if web_info else ""),
                "web_scraped_info": web_info,
                "image_base64": image_base64,
                "timestamp": datetime.utcnow().isoformat()
            }
            collection.insert_one(record)

            # Return result
            results.append({
                "filename": filename,
                "product_name": product_name,
                "category": category,
                "catalog_entry": record["catalog_entry"]
            })

        except Exception as e:
            results.append({"filename": file.filename, "error": str(e)})

    return jsonify(results)

# ==== Route: Get All Entries ====
@app.route("/entries", methods=["GET"])
def get_entries():
    all_entries = list(collection.find({}, {"_id": 0}))
    return jsonify(all_entries)

# ==== Route: Export PDF ====
@app.route("/export_pdf", methods=["GET"])
def export_pdf():
    entries = list(collection.find({}))
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    y = height - 40

    for entry in entries:
        try:
            # Add image
            img_data = base64.b64decode(entry["image_base64"])
            img = Image.open(io.BytesIO(img_data))
            aspect = img.width / img.height
            img_width = width - 80
            img_height = img_width / aspect
            if img_height > 300:
                img_height = 300
                img_width = img_height * aspect
            c.drawImage(ImageReader(img), 40, y - img_height, width=img_width, height=img_height)
            y -= img_height + 10

            # Add text
            text_obj = c.beginText(40, y)
            text_obj.setFont("Helvetica", 10)
            for line in entry["catalog_entry"].splitlines():
                if y < 100:
                    c.drawText(text_obj)
                    c.showPage()
                    text_obj = c.beginText(40, height - 40)
                    text_obj.setFont("Helvetica", 10)
                    y = height - 40
                text_obj.textLine(line)
                y -= 12
            c.drawText(text_obj)
            y -= 40
            if y < 200:
                c.showPage()
                y = height - 40
        except Exception:
            continue

    c.save()
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name="catalog.pdf", mimetype="application/pdf")

# ==== Run ====
if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8080))  # 👈 Use PORT from Railway
    app.run(host="0.0.0.0", port=port)

