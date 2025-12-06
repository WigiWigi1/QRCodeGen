import os
import json
import uuid
import base64
from io import BytesIO
from datetime import datetime
import stripe

from flask import (
    Flask, render_template, request, jsonify, send_file,
    send_from_directory, redirect, url_for, session, flash
)
from dotenv import load_dotenv
from authlib.integrations.flask_client import OAuth
from werkzeug.utils import secure_filename

# --- ДОБАВЛЕНО: SQLAlchemy ---
from flask_sqlalchemy import SQLAlchemy
# -----------------------------

import qrcode
from qrcode.image.pil import PilImage
from qrcode.image.svg import SvgPathImage

from PIL import Image, ImageDraw, ImageFont, ImageFilter


# ---------------------- CONFIG ----------------------
load_dotenv()

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret")
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.getcwd(), "data"))
os.makedirs(DATA_DIR, exist_ok=True)

# --- КОНФИГУРАЦИЯ БАЗЫ ДАННЫХ (SQLite) ---
DB_PATH = os.path.join(DATA_DIR, "site.db")
# Устанавливаем путь к файлу SQLite
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app) # Создаем объект DB
# ----------------------------------------

DYN_PATH = os.environ.get("DYN_PATH", os.path.join(DATA_DIR, "dynamic.json"))
if not os.path.exists(DYN_PATH):
    with open(DYN_PATH, "w", encoding="utf-8") as f:
        json.dump({}, f)

# Google OAuth (no passwords stored)
oauth = OAuth(app)
oauth.register(
    name="google",
    client_id=os.environ.get("GOOGLE_CLIENT_ID"),
    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
    access_token_url="https://oauth2.googleapis.com/token",
    authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
    api_base_url="https://www.googleapis.com/oauth2/v3/",
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

PREMIUM_EMAILS = {
    e.strip().lower() for e in os.environ.get("PREMIUM_EMAILS", "").split(",") if e.strip()
}

ALLOWED_ICON_EXT = {".png"}
MAX_ICON_SIZE_BYTES = 512 * 1024
MAX_ICON_DIM = 512

# ---------------------- МОДЕЛЬ DB ----------------------
class UserStatus(db.Model):
    """
    Модель для хранения постоянного статуса доступа пользователя.
    """
    id = db.Column(db.Integer, primary_key=True)
    # Используем нижний регистр для email
    email = db.Column(db.String(120), unique=True, nullable=False)
    has_one_time_access = db.Column(db.Boolean, default=False, nullable=False)

    def __repr__(self):
        return f"<UserStatus {self.email}: {'Paid' if self.has_one_time_access else 'Free'}>"

# ---------------------- HELPERS ----------------------
def current_user():
    return session.get("user")

def get_or_create_user(email: str):
    """
    Находит пользователя по email в DB или создает нового.
    """
    email_lower = email.lower()
    # Используем безопасный метод поиска для SQLAlchemy 2.0
    user_status = db.session.execute(
        db.select(UserStatus).filter_by(email=email_lower)
    ).scalar_one_or_none()

    if user_status is None:
        user_status = UserStatus(email=email_lower, has_one_time_access=False)
        db.session.add(user_status)
        # Commit не требуется здесь, если его вызывает auth_callback/payment_success
        # Но для безопасности добавим
        db.session.commit()
    return user_status

def is_pro() -> bool:
    u = current_user()
    return bool(u and u.get("email", "").lower() in PREMIUM_EMAILS)


def is_one_time() -> bool:
    # Статус берется напрямую из сессии, обновляется из DB при входе
    return session.get("one_time", False)


def is_paid() -> bool:
    return bool(is_pro() or is_one_time())


def normalize_url(link: str) -> str:
    v = (link or "").strip()
    if not v:
        return ""
    low = v.lower()
    if low.startswith("http://") or low.startswith("https://"):
        return v
    return "https://" + v


def _hex_to_rgb(h: str):
    h = (h or "#000000").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return (0, 0, 0)


def _mix(c_back, c_fill, ratio: float = 0.18):
    rb, gb, bb = c_back
    rf, gf, bf = c_fill
    r = int(rb * (1 - ratio) + rf * ratio)
    g = int(gb * (1 - ratio) + gf * ratio)
    b = int(bb * (1 - ratio) + bf * ratio)
    return (r, g, b)


def _srgb_gamma(c_norm: float) -> float:
    """Гамма-коррекция для нормализованного RGB (0-1)."""
    if c_norm <= 0.03928:
        return c_norm / 12.92
    else:
        return ((c_norm + 0.055) / 1.055) ** 2.4


def _get_luminance(rgb: tuple[int, int, int]) -> float:
    """Расчет относительной яркости (L) по WCAG."""
    r_s = _srgb_gamma(rgb[0] / 255.0)
    g_s = _srgb_gamma(rgb[1] / 255.0)
    b_s = _srgb_gamma(rgb[2] / 255.0)
    return 0.2126 * r_s + 0.7152 * g_s + 0.0722 * b_s


def _check_contrast(c1_hex: str, c2_hex: str, min_ratio: float = 4.5) -> bool:
    """Проверка коэффициента контрастности (CR)."""
    rgb1 = _hex_to_rgb(c1_hex)
    rgb2 = _hex_to_rgb(c2_hex)

    L1 = _get_luminance(rgb1)
    L2 = _get_luminance(rgb2)

    L_max = max(L1, L2)
    L_min = min(L1, L2)

    CR = (L_max + 0.05) / (L_min + 0.05)
    return CR >= min_ratio


def _tint_icon_png_to_color(png_rgba: Image.Image, rgb) -> Image.Image:
    icon = png_rgba.convert("RGBA")
    r, g, b, a = icon.split()
    colored = Image.new("RGBA", icon.size, (*rgb, 255))
    colored.putalpha(a)
    return colored


def _draw_badge(base_img: Image.Image, box, radius: int, fill_hex: str, back_hex: str):
    x0, y0, x1, y1 = box
    w = x1 - x0
    h = y1 - y0
    plate_rgb = _mix(_hex_to_rgb(back_hex), _hex_to_rgb(fill_hex), 0.18)
    outline_rgba = (*_hex_to_rgb(fill_hex), 110)

    shadow_pad = 6
    shadow = Image.new("RGBA", (w + shadow_pad * 2, h + shadow_pad * 2), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow)
    sdraw.rounded_rectangle((shadow_pad, shadow_pad, shadow_pad + w, shadow_pad + h),
                            radius=radius, fill=(0, 0, 0, 80))
    shadow = shadow.filter(ImageFilter.GaussianBlur(6))

    plate = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    pdraw = ImageDraw.Draw(plate)
    pdraw.rounded_rectangle((0, 0, w, h), radius=radius, fill=(*plate_rgb, 255), outline=outline_rgba,
                            width=max(2, w // 28))

    inner = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    idraw = ImageDraw.Draw(inner)
    inset = max(1, w // 36)
    idraw.rounded_rectangle((inset, inset, w - inset, h - inset), radius=max(1, radius - inset),
                            outline=(255, 255, 255, 90), width=1)

    base = base_img.copy()
    base.alpha_composite(shadow, (x0 - shadow_pad, y0 - shadow_pad))
    base.alpha_composite(plate, (x0, y0))
    base.alpha_composite(inner, (x0, y0))
    return base


def _overlay_wifi_png(img: Image.Image, fill_hex: str, back_hex: str) -> Image.Image:
    img = img.convert("RGBA")
    W, H = img.size
    side = int(min(W, H) * 0.24)
    half = side // 2
    cx, cy = W // 2, H // 2
    radius = int(side * 0.24)
    box = (cx - half, cy - half, cx + half, cy + half)

    img = _draw_badge(img, box, radius, fill_hex=fill_hex, back_hex=back_hex)

    path = os.path.join(app.static_folder, "icons", "wifi.png")
    if os.path.exists(path):
        base_png = Image.open(path).convert("RGBA")
        colored_png = _tint_icon_png_to_color(base_png, _hex_to_rgb(fill_hex))
        target = int(side * 1.1)
        colored_png.thumbnail((target, target), Image.LANCZOS)
        iw, ih = colored_png.size
        img.alpha_composite(colored_png, (cx - iw // 2, cy - ih // 2))
    return img


def _overlay_user_png(img: Image.Image, fill_hex: str, back_hex: str, custom_icon_path: str | None) -> Image.Image:
    img = img.convert("RGBA")
    W, H = img.size
    side = int(min(W, H) * 0.24)
    half = side // 2
    cx, cy = W // 2, H // 2
    radius = int(side * 0.24)
    box = (cx - half, cy - half, cx + half, cy + half)

    img = _draw_badge(img, box, radius, fill_hex=fill_hex, back_hex=back_hex)

    icon_img = None
    if custom_icon_path and os.path.exists(custom_icon_path):
        try:
            icon_img = Image.open(custom_icon_path).convert("RGBA")
        except Exception:
            icon_img = None
    if icon_img is None:
        fallback = os.path.join(app.static_folder, "icons", "user.png")
        if os.path.exists(fallback):
            icon_img = Image.open(fallback).convert("RGBA")

    if icon_img is not None:
        icon_img = _tint_icon_png_to_color(icon_img, _hex_to_rgb(fill_hex))
        target = int(side * 1.1)
        icon_img.thumbnail((target, target), Image.LANCZOS)
        iw, ih = icon_img.size
        img.alpha_composite(icon_img, (cx - iw // 2, cy - ih // 2))
    return img

def _load_ttf(px: int) -> ImageFont.FreeTypeFont | None:
    candidates: list[str] = []

    here = os.path.dirname(os.path.abspath(__file__))
    candidates += [
        os.path.join(here, "static", "fonts", "Inter-Medium.ttf"),
        os.path.join(here, "static", "fonts", "DejaVuSans.ttf"),
    ]

    try:
        import PIL
        pil_dir = os.path.dirname(PIL.__file__)
        candidates += [
            os.path.join(pil_dir, "fonts", "DejaVuSans.ttf"),
            os.path.join(pil_dir, "DejaVuSans.ttf"),
        ]
    except Exception:
        pass

    candidates += [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
    ]

    for p in candidates:
        try:
            if os.path.exists(p):
                return ImageFont.truetype(p, px)
        except Exception:
            continue

    try:
        return ImageFont.truetype("DejaVuSans.ttf", px)
    except Exception:
        return None


def _add_watermark_border(
        img: Image.Image,
        text: str = "Created by ColorQR.app",
        back_hex: str = "#ffffff",
        fill_hex: str = "#111111",
        font_scale: float = 0.05,
        margin_scale: float = 0.05,
        gap_scale: float = 0.05
) -> Image.Image:
    img = img.convert("RGBA")
    W, H = img.size
    side = min(W, H)

    m = max(int(side * margin_scale), 24)

    back_rgb = _hex_to_rgb(back_hex)
    frame = Image.new("RGBA", (W + 2 * m, H + 2 * m), (*back_rgb, 255))
    frame.alpha_composite(img, (m, m))

    fpx = max(int(side * float(font_scale)), 22)

    font = _load_ttf(fpx)
    fallback = False
    if font is None:
        fallback = True
        font = ImageFont.load_default()

    def lum(rgb):
        r, g, b = [c / 255.0 for c in rgb]; return 0.2126 * r + 0.7152 * g + 0.0722 * b

    text_col = (30, 30, 30, 240) if lum(back_rgb) > 0.6 else (245, 245, 245, 240)
    stroke_col = (255, 255, 255, 210) if lum(back_rgb) <= 0.6 else (0, 0, 0, 210)
    stroke_w = max(1, (fpx // 14) if not fallback else (fpx // 10))

    def make_block(rot=0):
        dtmp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        tw = int(dtmp.textlength(text, font=font))
        th = int((getattr(font, "size", 12)) * 1.2)

        blk = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
        d2 = ImageDraw.Draw(blk)
        d2.text((tw // 2, th // 2), text, font=font, anchor="mm",
                fill=text_col, stroke_width=stroke_w, stroke_fill=stroke_col)

        if fallback:
            target_h = int(fpx * 1.2)
            target_w = max(1, int(blk.width * (target_h / max(1, blk.height))))
            blk = blk.resize((target_w, target_h), Image.LANCZOS)

        if rot:
            blk = blk.rotate(rot, expand=True)
        return blk

    block_h = make_block(0)
    block_v = make_block(90)

    gap_h = max(int((W + 2 * m) * float(gap_scale)), int(block_h.width * 0.4))
    gap_v = max(int((H + 2 * m) * float(gap_scale)), int(block_v.height * 0.4))

    strip_h_h = max(int(fpx * 1.35), block_h.height + 2)

    def tile_h(y_top):
        strip = Image.new("RGBA", (W + 2 * m, strip_h_h), (0, 0, 0, 0))
        x = -block_h.width
        y = strip_h_h // 2 - block_h.height // 2
        while x < (W + 2 * m) - block_h.width + gap_h:
            strip.alpha_composite(block_h, (x, y))
            x += block_h.width + gap_h
        frame.alpha_composite(strip, (0, y_top))

    strip_v_w = max(int(fpx * 1.10), block_v.width + 2)

    def tile_v(x_left):
        strip = Image.new("RGBA", (strip_v_w, H + 2 * m), (0, 0, 0, 0))
        y = -block_v.height
        x = strip_v_w // 2 - block_v.width // 2
        while y < (H + 2 * m) - block_v.height + gap_v:
            strip.alpha_composite(block_v, (x, y))
            y += block_v.height + gap_v
        frame.alpha_composite(strip, (x_left, 0))

    tile_h(0)
    tile_h(H + m)
    tile_v(0)
    tile_v(W + m)

    return frame


def _save_jpg_from_rgba(pil_rgba: Image.Image, quality: int = 90) -> bytes:
    bg = Image.new("RGB", pil_rgba.size, (255, 255, 255))
    bg.paste(pil_rgba, mask=pil_rgba.split()[-1])
    buf = BytesIO()
    bg.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _gen_svg_bytes(data: str, fill_color: str, back_color: str) -> bytes:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
        image_factory=SvgPathImage
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color=fill_color, back_color=back_color)
    out = BytesIO()
    img.save(out)
    return out.getvalue()


# ---------------------- CONTENT (FAQ/Blog) ----------------------
POSTS = [
    {
        "slug": "qr-wifi-best-practices",
        "title": "Wi-Fi QR: Best Practices",
        "date": datetime(2025, 9, 2),
        "desc": "Sizes, contrast and icon tips so guests connect instantly.",
        "content": "<p>Use high contrast, keep center icon modest, and print at least 25 mm on edge...</p>",
    },
    {
        "slug": "branded-qr-design",
        "title": "Designing Branded QR Codes",
        "date": datetime(2025, 9, 12),
        "desc": "How to keep QR scannable while matching your brand.",
        "content": "<p>Pick palettes with strong luminance difference, test on real phones, prefer vector (SVG) for print...</p>",
    },
]

FAQS = [
    {"q": "Is ColorQR free?", "a": "Yes, the core features (URL, Wi-Fi, Text) are free."},
    {"q": "Do you store passwords or logins?", "a": "No. We only use Google Sign-In. No passwords are stored at all."},
    {"q": "What’s Pro vs One-Time?",
     "a": "One-Time unlock gives high-quality JPG and extra palettes for a session. Pro adds SVG export, Dynamic QR, vCard with icon and more."},
    {"q": "Do QR codes expire?", "a": "Static codes never expire. Dynamic QR (Pro) can be edited anytime."},
    {"q": "Will a center icon affect scanning?",
     "a": "We use safe sizes and H error correction. Always test before print."},
    {"q": "Can I change colors?", "a": "Free includes 4 palettes. Paid unlocks 16 extra palettes and custom color."},
]


# ---------------------- ROUTES: PAGES ----------------------
def tpl_args(active):
    post_login_msg = session.pop("post_login_redirect", False)
    return dict(
        active=active,
        user=current_user(),
        is_pro=is_pro(),
        is_one_time=is_one_time(),
        is_paid=is_paid(),
        show_vcard=is_pro(),
        show_dynamic=is_pro(),
        extra_palettes=is_paid(),
        post_login_msg=post_login_msg,
    )


@app.route("/")
def home():
    return render_template("index.html", **tpl_args("home"))


@app.route("/about")
def about():
    return render_template("about.html", **tpl_args("about"))


@app.route("/pricing")
def pricing():
    stripe_pub_key = os.environ.get("STRIPE_PUBLISHABLE_KEY")
    price_cents = os.environ.get("ONE_TIME_PRICE_CENTS", "199")

    return render_template(
        "pricing.html",
        stripe_pub_key=stripe_pub_key,
        one_time_price=int(price_cents) / 100,
        **tpl_args("pricing")
    )


@app.route("/contact")
def contact():
    return render_template("contact.html", **tpl_args("contact"))


@app.route("/faq")
def faq():
    return render_template("faq.html", **tpl_args("faq"), faqs=FAQS)


@app.route("/blog")
def blog():
    posts = sorted(POSTS, key=lambda p: p["date"], reverse=True)
    return render_template("blog.html", **tpl_args("blog"), posts=posts)


@app.route("/blog/<slug>")
def blog_post(slug):
    post = next((p for p in POSTS if p["slug"] == slug), None)
    if not post:
        return redirect(url_for("blog"))
    return render_template("post.html", **tpl_args("blog"), post=post)


@app.route("/robots.txt")
def robots():
    return send_from_directory(app.static_folder, "robots.txt")


@app.route("/sitemap.xml")
def sitemap():
    return send_from_directory(app.static_folder, "sitemap.xml")


# ---------------------- AUTH ----------------------
@app.route("/login")
def login():
    session["next_url"] = request.args.get("next", url_for("home"))

    redirect_uri = os.environ.get("OAUTH_REDIRECT_URI", url_for("auth_callback", _external=True))
    return oauth.google.authorize_redirect(redirect_uri)


@app.route("/auth/callback")
def auth_callback():
    token = oauth.google.authorize_access_token()
    userinfo = oauth.google.get('userinfo').json()

    user_email = userinfo.get("email")

    session["user"] = {
        "email": user_email,
        "name": userinfo.get("name"),
        "picture": userinfo.get("picture"),
    }

    # --- КРИТИЧЕСКОЕ ИЗМЕНЕНИЕ: СЧИТЫВАЕМ СТАТУС ИЗ DB И ЗАПИСЫВАЕМ В СЕССИЮ ---
    # Проверяем и создаем пользователя в DB, если он существует
    if user_email:
        user_status = get_or_create_user(user_email)
        # Если в DB есть платный доступ, активируем временный флаг в сессии
        if user_status.has_one_time_access:
            session["one_time"] = True
        else:
            session["one_time"] = False  # Гарантируем, что флаг сброшен, если пользователь не платил
    # --------------------------------------------------------------------------

    next_url = session.pop("next_url", url_for("home"))
    if next_url == url_for("pricing"):
        session["post_login_redirect"] = True

    return redirect(next_url)


# Stripe checkout
@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    price_cents = int(os.environ.get("ONE_TIME_PRICE_CENTS", 199))

    session_data = {
        "payment_method_types": ["card"],
        "line_items": [
            {
                "price_data": {
                    "currency": "eur",
                    "unit_amount": price_cents,
                    "product_data": {
                        "name": "ColorQR One-Time Access",
                        "description": "Unlocks high-quality QR exports and custom colors.",
                    },
                },
                "quantity": 1,
            }
        ],
        "success_url": url_for("payment_success", _external=True) + "?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": url_for("pricing", _external=True),
        "mode": "payment",
    }

    user_email = session.get("user", {}).get("email")
    if user_email:
        session_data["customer_email"] = user_email

    try:
        checkout_session = stripe.checkout.Session.create(**session_data)
        return jsonify({"sessionId": checkout_session.id, "url": checkout_session.url})
    except Exception as e:
        app.logger.error(f"Stripe session creation failed: {e}")
        return jsonify({"error": str(e)}), 400


# Stripe Payment success
@app.route("/payment-success")
def payment_success():
    session_id = request.args.get("session_id")
    if not session_id:
        return redirect(url_for("pricing"))

    if not os.environ.get("STRIPE_SECRET_KEY"):
        app.logger.error("STRIPE_SECRET_KEY is missing!")
        return redirect(url_for("pricing", error="Configuration Error"))

    try:
        stripe_session = stripe.checkout.Session.retrieve(session_id)

        if stripe_session.payment_status == "paid":

            session["one_time"] = True  # Активируем временный флаг

            user_email = stripe_session.get("customer_email")
            if not user_email and session.get("user"):
                user_email = session["user"].get("email")

            # --- КРИТИЧЕСКОЕ ИЗМЕНЕНИЕ: ЗАПИСЫВАЕМ СТАТУС В DB ---
            if user_email:
                user_status = get_or_create_user(user_email)
                user_status.has_one_time_access = True
                db.session.commit()
                flash(f"One-Time Access successfully linked to {user_email}. Access is now permanent for this account.")
            else:
                flash(
                    "One-Time Access activated for this session. Please log in to permanently link access to your Google account.")
            # -----------------------------------------------------

            return render_template("payment_success.html", **tpl_args("pricing"))
        else:
            return redirect(url_for("pricing", error="Payment not successful"))

    except Exception as e:
        app.logger.error(f"Stripe session retrieval failed: {e}")
        return redirect(url_for("pricing", error="Verification failed"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


# One-time demo toggle
@app.route("/unlock-one-time")
def unlock_one_time():
    session["one_time"] = True
    return redirect(url_for("pricing"))


@app.route("/lock-one-time")
def lock_one_time():
    session.pop("one_time", None)
    return redirect(url_for("pricing"))


@app.route("/api/check_contrast", methods=["POST"])
def check_contrast_api():
    payload = request.get_json(force=True, silent=True) or {}
    fill_color = payload.get("fill_color", "#000000")
    back_color = payload.get("back_color", "#ffffff")

    # Используем вашу существующую функцию для проверки WCAG AA (min 4.5)
    is_safe = _check_contrast(fill_color, back_color, min_ratio=4.5)

    # Дополнительно можно вернуть и сам коэффициент, если нужно
    return jsonify({"safe": is_safe})


# ---------------------- ICON UPLOAD (Pro only, для vCard) ----------------------
@app.route("/upload_icon", methods=["POST"])
def upload_icon():
    if not is_pro():
        return jsonify({"error": "Pro required"}), 403

    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_ICON_EXT:
        return jsonify({"error": "Only PNG is supported"}), 400

    f.seek(0, os.SEEK_END)
    size = f.tell()
    f.seek(0)
    if size > MAX_ICON_SIZE_BYTES:
        return jsonify({"error": "File too large (max 512KB)"}), 400

    icons_dir = os.path.join(DATA_DIR, "icons")
    os.makedirs(icons_dir, exist_ok=True)
    token = uuid.uuid4().hex
    filename = secure_filename(f"{token}.png")
    path = os.path.join(icons_dir, filename)

    try:
        im = Image.open(f).convert("RGBA")
        im.thumbnail((MAX_ICON_DIM, MAX_ICON_DIM), Image.LANCZOS)
        im.save(path, format="PNG")
    except Exception:
        return jsonify({"error": "Invalid image"}), 400

    session["custom_icon_path"] = path
    return jsonify({"ok": True, "token": token})


# ---------------------- DYNAMIC QR (Pro) ----------------------
@app.route("/dynamic/create", methods=["POST"])
def dynamic_create():
    if not is_pro():
        return jsonify({"error": "Pro required"}), 403
    payload = request.get_json(force=True, silent=True) or {}
    target = normalize_url(payload.get("target") or "")
    if not target:
        return jsonify({"error": "Target URL required"}), 400
    id_ = uuid.uuid4().hex[:8]
    with open(DYN_PATH, "r+", encoding="utf-8") as f:
        data = json.load(f)
        data[id_] = {"url": target, "created": datetime.utcnow().isoformat()}
        f.seek(0);
        json.dump(data, f);
        f.truncate()
    # Возвращаем короткую ссылку
    short = url_for("dynamic_redirect", id=id_, _external=True)
    return jsonify({"id": id_, "short": short})


@app.route("/r/<id>")
def dynamic_redirect(id):
    with open(DYN_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    item = data.get(id)
    if not item:
        return redirect("https://colorqr.app/")
    return redirect(item["url"])


# ---------------------- QR GENERATION ----------------------
@app.route("/generate_qr", methods=["POST"])
def generate_qr():
    payload = request.get_json(force=True, silent=True) or {}
    data_type = (payload.get("data_type") or "url").lower()  # url | wifi | text | vcard | dynamic
    raw = (payload.get("data") or "").strip()

    # доступность типов
    if data_type == "vcard" and not is_pro():
        return jsonify({"error": "vCard available in Pro"}), 403
    if data_type == "dynamic" and not is_pro():
        return jsonify({"error": "Dynamic QR available in Pro"}), 403

    if not raw:
        return jsonify({"error": "Data is required"}), 400

    if data_type in ("url", "dynamic"):
        raw = normalize_url(raw)

    fill_color = payload.get("fill_color", "#000000")
    back_color = payload.get("back_color", "#ffffff")
    size_key = payload.get("size", "md")  # sm | md | lg
    # размеры: Free → sm/md; Paid → +lg
    if size_key == "lg" and not is_paid():
        size_key = "md"

    # --- CRITICAL FIX 1: Проверка контрастности для платных тарифов ---
    # Проверка нужна только там, где пользователь мог выбрать кастомный цвет (т.е. Paid).
    if is_paid():
        # Рекомендованный минимум 4.5:1 для обеспечения сканируемости.
        if not _check_contrast(fill_color, back_color, min_ratio=4.5):
            return jsonify({
                "error": "Color contrast is too low (min 4.5:1 required). Please choose a darker foreground or lighter background for reliable scanning."
            }), 400
    # ------------------------------------------------------------------

    px = {"sm": 256, "md": 512, "lg": 1024}.get(size_key, 512)
    box = 10 if px >= 512 else 8

    # Make QR (H correction)
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=box, border=4)
    qr.add_data(raw)
    qr.make(fit=True)
    img: PilImage = qr.make_image(fill_color=fill_color, back_color=back_color).convert("RGBA")

    # Иконки:
    if data_type == "wifi":
        img = _overlay_wifi_png(img, fill_hex=fill_color, back_hex=back_color)
    elif data_type == "vcard" and is_pro():
        img = _overlay_user_png(img, fill_hex=fill_color, back_hex=back_color,
                                custom_icon_path=session.get("custom_icon_path"))

    # Водяной знак для Free

    # финальный размер
    img = img.resize((px, px), Image.LANCZOS)

    # watermark только для Free
    if not is_paid():
        img = _add_watermark_border(
            img,
            text="Created by ColorQR.app",
            back_hex=back_color,
            fill_hex=fill_color,
            font_scale=0.05,
            margin_scale=0.05,
            gap_scale=0.05
        )

    # без повторного resize!

    # Сохранение JPG
    uid = str(uuid.uuid4())
    jpg_bytes = _save_jpg_from_rgba(img, quality=(95 if is_one_time() or is_pro() else 88))

    # Сохранение JPG (для превью/скачивания)
    uid = str(uuid.uuid4())
    jpg_bytes = _save_jpg_from_rgba(img, quality=(95 if is_one_time() or is_pro() else 88))
    with open(os.path.join(DATA_DIR, f"{uid}.jpg"), "wb") as f:
        f.write(jpg_bytes)

    svg_available = False
    # SVG только для Pro
    if is_pro():
        try:
            svg_bytes = _gen_svg_bytes(raw, fill_color, back_color)
            with open(os.path.join(DATA_DIR, f"{uid}.svg"), "wb") as f:
                f.write(svg_bytes)
                svg_available = True  # SVG успешно сгенерирован
        except Exception:
            pass

    b64 = base64.b64encode(jpg_bytes).decode("utf-8")

    # --- FIX: Сохраняем имя файла для скачивания ---
    download_name = f"QR_{data_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    session["download_name"] = download_name

    return jsonify({
        "qr_code": b64,
        "id": uid,
        "svg_available": svg_available  # Фронтенд увидит, что можно скачать SVG
    })


# ---------------------- DOWNLOADS ----------------------
@app.route("/download_jpg")
def download_jpg():
    file_id = request.args.get("id")
    if not file_id:
        return "Missing id", 400

    # --- FIX: Используем имя из сессии, если есть ---
    download_name = session.pop("download_name", "qrcode")
    path = os.path.join(DATA_DIR, f"{file_id}.jpg")
    if not os.path.exists(path):
        return "Not found", 404
    return send_file(path, as_attachment=True, download_name=f"{download_name}.jpg", mimetype="image/jpeg")


@app.route("/download_svg")
def download_svg():
    if not is_pro():
        return "Pro required", 403
    file_id = request.args.get("id")
    if not file_id:
        return "Missing id", 400

    # --- FIX: Используем имя из сессии, если есть ---
    download_name = session.pop("download_name", "qrcode")

    path = os.path.join(DATA_DIR, f"{file_id}.svg")
    if not os.path.exists(path):
        return "Not found", 404

    # Используем новое имя
    return send_file(path, as_attachment=True, download_name=f"{download_name}.svg", mimetype="image/svg+xml")


# ---------------------- DEV ENTRY ----------------------
@app.route("/whoami")
def whoami():
    return {
        "is_pro": is_pro(),
        "is_one_time": is_one_time(),
        "is_paid": is_paid(),
        "user": session.get("user"),
    }


if __name__ == "__main__":
    with app.app_context():
        db.create_all()  # <-- Эта строка создает таблицу user_status
    host = "127.0.0.1"
    port = int(os.environ.get("PORT", 5000))
    print(f"➡  Local server: http://{host}:{port}  (Ctrl+C to stop)")
    app.run(host=host, port=port, debug=True)