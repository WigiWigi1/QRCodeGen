from flask import Flask, request, jsonify
import qrcode
import base64
from io import BytesIO
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # Allow cross-origin requests


@app.route('/generate_qr', methods=['GET'])
def generate_qr():
    link = request.args.get('link', '')
    if not link:
        return jsonify({"error": "No link provided"}), 400

    # Generate QR code
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(link)
    qr.make(fit=True)

    # Convert QR code to base64
    img = qr.make_image(fill_color="black", back_color="white")
    buffered = BytesIO()
    img.save(buffered, format="PNG")
    img_str = base64.b64encode(buffered.getvalue()).decode('utf-8')

    return jsonify({"qr_code": img_str})


if __name__ == '__main__':
    app.run(debug=True)
