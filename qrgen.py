from flask import Flask, render_template, request, jsonify
import qrcode
from qrcode.image.pil import PilImage
import base64
from io import BytesIO

app = Flask(__name__)

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/generate_qr", methods=["POST"])
def generate_qr():
    data = request.json
    link = data.get("link")
    fill_color = data.get("fill_color", "black")
    back_color = data.get("back_color", "white")

    if not link:
        return jsonify({"error": "No link provided"}), 400

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(link)
    qr.make(fit=True)

    img: PilImage = qr.make_image(fill_color=fill_color, back_color=back_color)
    buf = BytesIO()
    img.save(buf, format="PNG")
    img_str = base64.b64encode(buf.getvalue()).decode("utf-8")

    return jsonify({"qr_code": img_str})
