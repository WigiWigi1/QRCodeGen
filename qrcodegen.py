from flask import Flask, render_template, request, jsonify, send_file
import qrcode
from qrcode.image.pil import PilImage
import base64
from io import BytesIO
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
import os
import re
import uuid
import time

app = Flask(__name__)

DATA_DIR = os.environ.get("DATA_DIR", "data")
os.makedirs(DATA_DIR, exist_ok=True)

HEX_RE = re.compile(r"^#([0-9a-fA-F]{6})$")

# Разрешённые размеры (итоговый размер PNG, px)
SIZE_MAP = {
    "sm": 200,
    "md": 300,
    "lg": 500,
}

def clean_old_files():
    """Удаляет PNG старше QR_MAX_AGE_HOURS (по умолчанию 24 ч)."""
    max_hours = int(os.environ.get("QR_MAX_AGE_HOURS", 24))
    cutoff = time.time() - max_hours * 3600
    try:
        for name in os.listdir(DATA_DIR):
            if not name.endswith(".png"):
                continue
            path = os.path.join(DATA_DIR, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except Exception:
                pass
    except FileNotFoundError:
        os.makedirs(DATA_DIR, exist_ok=True)

@app.route("/health", methods=["GET"])
def health():
    clean_old_files()
    return jsonify({"status": "ok"}), 200

@app.route("/")
def home():
    return render_template("index.html")

def _safe_color(c: str, default: str) -> str:
    if isinstance(c, str) and HEX_RE.match(c):
        return c
    return default

def _normalize_link(link: str) -> str:
    link = (link or "").strip()
    if not link:
        return ""
    low = link.lower()
    if not (low.startswith("http://") or low.startswith("https://")):
        link = "https://" + link
    return link

@app.route("/generate_qr", methods=["POST"])
def generate_qr():
    # лёгкая уборка
    clean_old_files()

    data = request.get_json(silent=True) or {}
    link = _normalize_link(data.get("link", ""))
    fill_color = _safe_color(data.get("fill_color", "#000000"), "#000000")
    back_color = _safe_color(data.get("back_color", "#FFFFFF"), "#FFFFFF")
    size_key = (data.get("size") or "md").lower()
    final_px = SIZE_MAP.get(size_key, SIZE_MAP["md"])

    if not link:
        return jsonify({"error": "No link provided"}), 400

    # генерим QR, даём библиотеке подобрать версию
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(link)
    qr.make(fit=True)

    img: PilImage = qr.make_image(fill_color=fill_color, back_color=back_color)

    # жёстко приводим к выбранному размеру (сохраняем резкость QR)
    from PIL import Image
    img = img.resize((final_px, final_px), resample=Image.NEAREST)

    # сохраняем на диск
    qr_id = uuid.uuid4().hex
    png_path = os.path.join(DATA_DIR, f"{qr_id}.png")
    img.save(png_path, format="PNG")

    # превью в base64 для мгновенного показа
    buf = BytesIO()
    img.save(buf, format="PNG")
    img_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

    return jsonify({"qr_code": img_b64, "id": qr_id})

@app.route("/download_png", methods=["GET"])
def download_png():
    qr_id = request.args.get("id", "")
    png_path = os.path.join(DATA_DIR, f"{qr_id}.png")
    if not qr_id or not os.path.isfile(png_path):
        return "QR not found", 404
    return send_file(png_path, mimetype="image/png", as_attachment=True, download_name="qrcode.png")

@app.route("/download_pdf", methods=["GET"])
def download_pdf():
    qr_id = request.args.get("id", "")
    png_path = os.path.join(DATA_DIR, f"{qr_id}.png")
    if not qr_id or not os.path.isfile(png_path):
        return "QR not found", 404

    pdf_buf = BytesIO()
    # Плотное поле под QR и подпись
    c = canvas.Canvas(pdf_buf, pagesize=(final_w := 360, final_h := 420))
    qr_img = ImageReader(png_path)
    c.drawImage(qr_img, 30, 150, 300, 300, preserveAspectRatio=True, anchor='sw')
    txt = c.beginText(30, 120)
    txt.setFont("Helvetica", 10)
    txt.textLine("Generated QR")
    c.drawText(txt)
    c.showPage()
    c.save()

    pdf_buf.seek(0)
    return send_file(pdf_buf, mimetype="application/pdf", as_attachment=True, download_name="qrcode.pdf")

# Однократная уборка при старте
clean_old_files()
