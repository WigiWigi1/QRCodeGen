import os
import json
import uuid
import base64
from io import BytesIO
from datetime import datetime
from urllib.parse import urlparse
import stripe
from flask import abort

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

DYNAMIC_QR_DIR = os.path.join(DATA_DIR, "dynamic_qr")
os.makedirs(DYNAMIC_QR_DIR, exist_ok=True)

# --- КОНФИГУРАЦИЯ БАЗЫ ДАННЫХ (SQLite) ---
DB_PATH = os.path.join(DATA_DIR, "site.db")
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)
# ----------------------------------------

DEBUG_MODE = os.environ.get("DEBUG_MODE", "false").lower() == "true"

DYN_PATH = os.environ.get("DYN_PATH", os.path.join(DATA_DIR, "dynamic.json"))
if not os.path.exists(DYN_PATH):
    with open(DYN_PATH, "w", encoding="utf-8") as f:
        json.dump({}, f)

def _create_dynamic_entry(target_url: str) -> tuple[str, str]:
    """
    Создаёт запись для Dynamic QR в DYN_PATH и возвращает (id, short_url).
    """
    id_ = uuid.uuid4().hex[:8]

    # читаем существующий JSON (или создаём новый)
    with open(DYN_PATH, "r+", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            data = {}

        data[id_] = {
            "url": target_url,
            "created": datetime.utcnow().isoformat()
        }

        f.seek(0)
        json.dump(data, f)
        f.truncate()

    # короткая ссылка вида https://colorqr.app/r/abcd1234
    short = url_for("dynamic_redirect", id=id_, _external=True)
    return id_, short



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

    is_sub_pro = db.Column(db.Boolean, default=False, nullable=False)
    stripe_customer_id = db.Column(db.String(120), nullable=True)
    stripe_subscription_id = db.Column(db.String(120), nullable=True)
    subscription_status = db.Column(db.String(50), nullable=True)  # 'active', 'canceled', etc.

    def __repr__(self):
        return f"<UserStatus {self.email}: {'Paid' if self.has_one_time_access else 'Free'}>"


class DynamicLink(db.Model):
    """
    Dynamic QR: хранит соответствие короткого id -> target_url + владелец.
    """
    id = db.Column(db.String(16), primary_key=True)          # короткий ID (например, 8 символов)
    owner_email = db.Column(db.String(120), index=True)      # владелец (Pro-пользователь)
    target_url = db.Column(db.String(1024), nullable=False)  # конечный URL
    label = db.Column(db.String(255))                        # подпись/название (опционально)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<DynamicLink {self.id} -> {self.target_url}>"


def _create_dynamic_link_in_db(target_url: str, label: str | None = None) -> tuple[str, str]:
    """
    Создаёт запись DynamicLink в БД и возвращает (id, short_url).
    """
    # владелец (если залогинен)
    u = current_user()
    owner_email = (u or {}).get("email")
    if owner_email:
        owner_email = owner_email.lower()
    else:
        owner_email = None

    # генерируем короткий id и убеждаемся, что он уникален в БД
    id_ = None
    for _ in range(5):
        candidate = uuid.uuid4().hex[:8]
        if db.session.get(DynamicLink, candidate) is None:
            id_ = candidate
            break
    if not id_:
        # можно кинуть исключение, чтобы поймать выше
        raise RuntimeError("Could not generate unique id")

    link = DynamicLink(
        id=id_,
        owner_email=owner_email,
        target_url=target_url,
        label=label,
    )
    db.session.add(link)
    db.session.commit()

    short = url_for("dynamic_redirect", id=id_, _external=True)
    return id_, short


# ---------------------- HELPERS ----------------------
def current_user():
    return session.get("user")


def get_or_create_user(email: str):
    """
    Находит пользователя по email в DB или создает нового.
    """
    email_lower = email.lower()
    user_status = db.session.execute(
        db.select(UserStatus).filter_by(email=email_lower)
    ).scalar_one_or_none()

    if user_status is None:
        user_status = UserStatus(email=email_lower, has_one_time_access=False)
        db.session.add(user_status)
        db.session.commit()
    return user_status


def is_pro() -> bool:
    u = current_user()
    email = (u or {}).get("email", "").lower()

    # 1) Admin / ручной Pro
    if email and email in PREMIUM_EMAILS:
        return True

    # 2) Pro через подписку или one-time в базе
    if email:
        user_status = db.session.execute(
            db.select(UserStatus).filter_by(email=email)
        ).scalar_one_or_none()
        if user_status:
            if user_status.is_sub_pro and user_status.subscription_status == "active":
                return True

    # 3) Dev-режим
    if session.get("pro_debug"):
        return True

    return False



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


def _normalize_hex(h: str) -> str:
    h = (h or "").strip()
    if not h:
        return "#000000"
    if not h.startswith("#"):
        h = "#" + h
    if len(h) == 4:
        h = "#" + "".join(c * 2 for c in h[1:])
    return h.lower()


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


SAFE_ICON_SCALE = 0.18  # было 0.24 – уменьшили, чтобы логотип не лез к краям


def _overlay_wifi_png(img: Image.Image, fill_hex: str, back_hex: str) -> Image.Image:
    img = img.convert("RGBA")
    W, H = img.size
    side = int(min(W, H) * SAFE_ICON_SCALE)
    half = side // 2
    cx, cy = W // 2, H // 2
    radius = int(side * 0.24)
    box = (cx - half, cy - half, cx + half, cy + half)

    img = _draw_badge(img, box, radius, fill_hex=fill_hex, back_hex=back_hex)

    path = os.path.join(app.static_folder, "icons", "wifi.png")
    if os.path.exists(path):
        base_png = Image.open(path).convert("RGBA")
        colored_png = _tint_icon_png_to_color(base_png, _hex_to_rgb(fill_hex))
        target = side  # без 1.1 – логотип строго в бейдже
        colored_png.thumbnail((target, target), Image.LANCZOS)
        iw, ih = colored_png.size
        img.alpha_composite(colored_png, (cx - iw // 2, cy - ih // 2))
    return img


def _overlay_user_png(img: Image.Image, fill_hex: str, back_hex: str, custom_icon_path: str | None) -> Image.Image:
    img = img.convert("RGBA")
    W, H = img.size
    side = int(min(W, H) * SAFE_ICON_SCALE)
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
        target = side  # без 1.1
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

    # отступы вокруг QR
    m = max(int(side * margin_scale), 24)

    # фон берём из back_hex, чтобы на цветных фонах всё было консистентно
    back_rgb = _hex_to_rgb(back_hex)
    frame = Image.new("RGBA", (W + 2 * m, H + 2 * m), (*back_rgb, 255))
    frame.alpha_composite(img, (m, m))

    # размер шрифта для нормального TTF
    fpx = max(int(side * float(font_scale)), 22)

    font = _load_ttf(fpx)
    fallback = False
    if font is None:
        # если нормального TTF нет — используем встроенный bitmap-шрифт
        fallback = True
        font = ImageFont.load_default()

    def lum(rgb):
        r, g, b = [c / 255.0 for c in rgb]
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    text_col = (30, 30, 30, 240) if lum(back_rgb) > 0.6 else (245, 245, 245, 240)
    stroke_col = (255, 255, 255, 210) if lum(back_rgb) <= 0.6 else (0, 0, 0, 210)

    # важный момент: при fallback обводку делаем тонкой, чтобы не замыливать мелкий шрифт
    stroke_w = max(1, (fpx // 14) if not fallback else 1)

    def make_block(rot=0):
        # считаем ширину текста
        dtmp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        tw = int(dtmp.textlength(text, font=font))
        th = int((getattr(font, "size", 12)) * 1.2)

        blk = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
        d2 = ImageDraw.Draw(blk)
        d2.text(
            (tw // 2, th // 2),
            text,
            font=font,
            anchor="mm",
            fill=text_col,
            stroke_width=stroke_w,
            stroke_fill=stroke_col,
        )

        # 🔴 ГЛАВНОЕ ИЗМЕНЕНИЕ:
        # больше НИКАКОГО ресайза bitmap-шрифта. Раньше тут был blk.resize(..., LANCZOS),
        # который и превращал текст в кашу при fallback.
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

    # верх/низ
    tile_h(0)
    tile_h(H + m)
    # левый/правый
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
    """
    Генерация SVG с нужной палитрой.
    Если библиотека qrcode игнорирует цвета, мы перекрашиваем SVG постфактум.
    """
    fill_color = _normalize_hex(fill_color)
    back_color = _normalize_hex(back_color)

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

    svg_text = out.getvalue().decode("utf-8", errors="ignore")

    # На случай, если библиотека всё равно оставила дефолтные цвета
    svg_text = svg_text.replace('fill="#000000"', f'fill="{fill_color}"')
    svg_text = svg_text.replace('fill="#ffffff"', f'fill="{back_color}"')

    return svg_text.encode("utf-8")


def _build_download_name(data_type: str, raw_data: str) -> str:
    """
    Строим красивое имя файла:
    - для URL/dynamic: домен (example.com-YYYYMMDD-HHMMSS)
    - для Wi-Fi/vCard: понятный префикс
    """
    base = "colorqr"

    if data_type in ("url", "dynamic"):
        try:
            parsed = urlparse(raw_data)
            host = parsed.netloc or ""
            host = host.replace("www.", "")
            if host:
                base = host.split(":")[0]
        except Exception:
            pass
    elif data_type == "wifi":
        base = "wifi-qr"
    elif data_type == "vcard":
        base = "vcard-qr"
    else:
        base = f"{data_type}-qr"

    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    return f"{base}-{ts}"


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
    {"q": "What is a Dynamic QR code?",
     "a": "A Dynamic QR contains a short redirect link instead of a fixed URL. The QR image never changes, but you can edit its destination anytime — even after printing or sharing it."},
    {"q": "What is an SVG export and why do I need it?",
     "a": "SVG is a vector format, which means your QR code stays perfectly sharp at any size — whether it's a tiny label or a large poster. Unlike JPG or PNG, SVG never loses quality and is ideal for high-resolution printing, graphic design and professional branding."}
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
    session.pop("custom_icon_path", None)
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

    if user_email:
        user_status = get_or_create_user(user_email)
        if user_status.has_one_time_access:
            session["one_time"] = True
        else:
            session["one_time"] = False

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


@app.route("/create-subscription-session", methods=["POST"])
def create_subscription_session():
    # Для подписки Pro пользователь логически должен быть залогинен
    user_email = session.get("user", {}).get("email")
    if not user_email:
        # можно редиректить на логин, но здесь вернём 401 и на фронте открыть /login
        return jsonify({"error": "Auth required"}), 401

    price_id = os.environ.get("STRIPE_PRO_MONTHLY_PRICE_ID")
    if not price_id:
        return jsonify({"error": "Missing STRIPE_PRO_MONTHLY_PRICE_ID"}), 500

    try:
        checkout_session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=url_for("payment_success", _external=True) + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=url_for("pricing", _external=True),
            customer_email=user_email,
            metadata={
                "user_email": user_email
            }
        )
        return jsonify({"sessionId": checkout_session.id, "url": checkout_session.url})
    except Exception as e:
        app.logger.error(f"Stripe subscription session creation failed: {e}")
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
        stripe_session = stripe.checkout.Session.retrieve(session_id, expand=["subscription"])

        # общий email
        user_email = stripe_session.get("customer_email")
        if not user_email and session.get("user"):
            user_email = session["user"].get("email")

        # One-time режим (как было)
        if stripe_session.mode == "payment":
            if stripe_session.payment_status == "paid":
                session["one_time"] = True
                if user_email:
                    user_status = get_or_create_user(user_email)
                    user_status.has_one_time_access = True
                    db.session.commit()
                    flash(f"One-Time Access successfully linked to {user_email}. Access is now permanent for this account.")
                else:
                    flash("One-Time Access activated for this session. Please log in to permanently link access to your Google account.")
                return render_template("payment_success.html", **tpl_args("pricing"))
            else:
                return redirect(url_for("pricing", error="Payment not successful"))

        # НОВОЕ: режим subscription
        if stripe_session.mode == "subscription":
            subscription = stripe_session.subscription  # уже expanded
            if user_email:
                user_status = get_or_create_user(user_email)
                user_status.is_sub_pro = True
                user_status.subscription_status = getattr(subscription, "status", "active")
                user_status.stripe_customer_id = stripe_session.customer
                user_status.stripe_subscription_id = subscription.id
                db.session.commit()

            flash("Your ColorQR Pro subscription is active. Welcome!")
            return render_template("payment_success.html", **tpl_args("pricing"))

        # fallback
        return redirect(url_for("pricing"))

    except Exception as e:
        app.logger.error(f"Stripe session retrieval failed: {e}")
        return redirect(url_for("pricing", error="Verification failed"))

@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET")

    if not webhook_secret:
        app.logger.error("STRIPE_WEBHOOK_SECRET not set")
        return "", 400

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=webhook_secret
        )
    except ValueError as e:
        # Invalid payload
        app.logger.error(f"Webhook payload error: {e}")
        return "", 400
    except stripe.error.SignatureVerificationError as e:
        # Invalid signature
        app.logger.error(f"Webhook signature error: {e}")
        return "", 400

    # Обрабатываем интересующие события
    event_type = event["type"]
    data = event["data"]["object"]

    # 1) Подписка создана / обновлена
    if event_type in ("customer.subscription.created", "customer.subscription.updated"):
        sub = data
        customer_id = sub["customer"]
        status = sub["status"]  # trialing, active, past_due, canceled, etc.

        user = db.session.execute(
            db.select(UserStatus).filter_by(stripe_customer_id=customer_id)
        ).scalar_one_or_none()

        if user:
            user.subscription_status = status
            user.is_sub_pro = status in ("trialing", "active")
            user.stripe_subscription_id = sub["id"]
            db.session.commit()

    # 2) Подписка отменена
    if event_type == "customer.subscription.deleted":
        sub = data
        customer_id = sub["customer"]

        user = db.session.execute(
            db.select(UserStatus).filter_by(stripe_customer_id=customer_id)
        ).scalar_one_or_none()

        if user:
            user.subscription_status = "canceled"
            user.is_sub_pro = False
            db.session.commit()

    return "", 200


@app.route("/billing-portal")
def billing_portal():
    """
    Отправляем пользователя в Stripe Customer Portal, где он может
    отменить подписку, поменять карту и т.п.
    """
    u = current_user()
    if not u or not u.get("email"):
        # Если не залогинен — логиним и возвращаемся на pricing
        return redirect(url_for("login", next=url_for("pricing")))

    email = u["email"].lower()
    user_status = db.session.execute(
        db.select(UserStatus).filter_by(email=email)
    ).scalar_one_or_none()

    if not user_status or not user_status.stripe_customer_id:
        # Нет привязки к Stripe customer → показываем сообщение
        flash("We could not find a Stripe customer for your account. Please contact support.")
        return redirect(url_for("pricing"))

    try:
        portal_session = stripe.billing_portal.Session.create(
            customer=user_status.stripe_customer_id,
            return_url=url_for("pricing", _external=True),
        )
        return redirect(portal_session.url)
    except Exception as e:
        app.logger.error(f"Stripe billing portal session creation failed: {e}")
        flash("Could not open billing portal. Please try again or contact support.")
        return redirect(url_for("pricing"))



@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))

if DEBUG_MODE:
    # One-time demo toggle
    @app.route("/unlock-one-time")
    def unlock_one_time():
        session["one_time"] = True
        return redirect(url_for("pricing"))


    @app.route("/lock-one-time")
    def lock_one_time():
        session.pop("one_time", None)
        return redirect(url_for("pricing"))


    @app.route("/unlock-pro")
    def unlock_pro():
        """Включить Pro в текущей сессии (dev-режим)."""
        session["pro_debug"] = True
        return redirect(url_for("pricing"))


    @app.route("/lock-pro")
    def lock_pro():
        """Выключить dev-Pro в текущей сессии."""
        session.pop("pro_debug", None)
        return redirect(url_for("pricing"))


@app.route("/api/check_contrast", methods=["POST"])
def check_contrast_api():
    payload = request.get_json(force=True, silent=True) or {}
    fill_color = payload.get("fill_color", "#000000")
    back_color = payload.get("back_color", "#ffffff")

    is_safe = _check_contrast(fill_color, back_color, min_ratio=4.5)
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


@app.route("/clear_icon", methods=["POST"])
def clear_icon():
    """
    Сбросить выбранную иконку (логотип) из сессии.
    Используется, когда пользователь нажимает «Remove icon».
    """
    if not is_pro():
        return jsonify({"error": "Pro required"}), 403

    session.pop("custom_icon_path", None)
    return jsonify({"ok": True})

# ---------------------- DYNAMIC QR (Pro) ----------------------
@app.route("/dynamic/create", methods=["POST"])
def dynamic_create():
    if not is_pro():
        return jsonify({"error": "Pro required"}), 403

    payload = request.get_json(force=True, silent=True) or {}
    target = normalize_url(payload.get("target") or "")
    label = (payload.get("label") or "").strip() or None

    if not target:
        return jsonify({"error": "Target URL required"}), 400

    try:
        id_, short = _create_dynamic_link_in_db(target, label)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"id": id_, "short": short})


@app.route("/r/<id>")
def dynamic_redirect(id):
    # 1. Сначала пробуем найти в БД (новый формат)
    link = db.session.get(DynamicLink, id)
    if link and link.target_url:
        return redirect(link.target_url)

    # 2. Fallback: старые динамические коды из JSON-файла
    try:
        with open(DYN_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        item = data.get(id)
        if item and "url" in item:
            return redirect(item["url"])
    except Exception:
        pass

    # 3. Если ничего не нашли — ведём просто на главную
    return redirect("https://colorqr.app/")


@app.route("/dynamic/manage")
def dynamic_manage():
    # Только для Pro
    if not is_pro():
        return redirect(url_for("pricing"))

    u = current_user()
    if not u or not u.get("email"):
        # если почему-то нет логина — отправляем логиниться
        return redirect(url_for("login", next=url_for("dynamic_manage")))

    email = u["email"].lower()

    links = db.session.execute(
        db.select(DynamicLink)
          .filter_by(owner_email=email)
          .order_by(DynamicLink.created_at.desc())
    ).scalars().all()

    # active='dynamic' — просто для подсветки/состояния, в меню пока не отображается
    return render_template(
        "dynamic_manage.html",
        **tpl_args("dynamic"),
        links=links
    )


@app.route("/dynamic/update/<id>", methods=["POST"])
def dynamic_update(id):
    if not is_pro():
        return jsonify({"error": "Pro required"}), 403

    u = current_user()
    if not u or not u.get("email"):
        return jsonify({"error": "Auth required"}), 401

    email = u["email"].lower()

    link = db.session.get(DynamicLink, id)
    # владелец может править только свои ссылки,
    # а "админ" из PREMIUM_EMAILS может править все
    if not link:
        return jsonify({"error": "Not found"}), 404
    if link.owner_email and link.owner_email != email and email not in PREMIUM_EMAILS:
        return jsonify({"error": "Not allowed"}), 403

    payload = request.get_json(force=True, silent=True) or {}
    target = normalize_url(payload.get("target_url") or "")
    if not target:
        return jsonify({"error": "Target URL required"}), 400
    label = (payload.get("label") or "").strip() or None

    link.target_url = target
    link.label = label
    db.session.commit()

    return jsonify({"ok": True})

@app.route("/dynamic/delete/<id>", methods=["POST"])
def dynamic_delete(id):
    if not is_pro():
        return jsonify({"error": "Pro required"}), 403

    u = current_user()
    if not u or not u.get("email"):
        return jsonify({"error": "Auth required"}), 401

    email = u["email"].lower()

    link = db.session.get(DynamicLink, id)
    if not link:
        return jsonify({"error": "Not found"}), 404

    # владелец может удалять только свои ссылки,
    # PREMIUM_EMAILS — админ, может удалять всё
    if link.owner_email and link.owner_email != email and email not in PREMIUM_EMAILS:
        return jsonify({"error": "Not allowed"}), 403

    db.session.delete(link)
    db.session.commit()

    # ВАЖНО: сам файл dynamic_qr/<id>.jpg не трогаем,
    # чтобы у юзера оставалась физическая картинка, если он её где-то использует.
    return jsonify({"ok": True})



# ---------------------- QR GENERATION ----------------------
@app.route("/generate_qr", methods=["POST"])
def generate_qr():
    payload = request.get_json(force=True, silent=True) or {}
    data_type = (payload.get("data_type") or "url").lower()  # url | wifi | text | vcard | dynamic
    raw = (payload.get("data") or "").strip()
    dynamic_id = None
    dynamic_short = None


    if data_type == "vcard" and not is_pro():
        return jsonify({"error": "vCard available in Pro"}), 403
    if data_type == "dynamic" and not is_pro():
        return jsonify({"error": "Dynamic QR available in Pro"}), 403

    if not raw:
        return jsonify({"error": "Data is required"}), 400

    if data_type == "url":
        raw = normalize_url(raw)



    elif data_type == "dynamic":
        # raw здесь — целевой URL, который хочет пользователь
        target = normalize_url(raw)
        if not target:
            return jsonify({"error": "Target URL required"}), 400
        try:
            # создаём запись в БД и короткую ссылку /r/<id>
            dynamic_id, dynamic_short = _create_dynamic_link_in_db(target, label=None)
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 500
        # в сам QR зашиваем именно короткую ссылку
        raw = dynamic_short
    # wifi/text/vcard — оставляем raw как есть


    fill_color = payload.get("fill_color", "#000000")
    back_color = payload.get("back_color", "#ffffff")

    size_key = payload.get("size", "md")  # sm | md | lg
    if size_key == "lg" and not is_paid():
        size_key = "md"

    if is_paid():
        if not _check_contrast(fill_color, back_color, min_ratio=4.5):
            return jsonify({
                "error": "Color contrast is too low (min 4.5:1 required). Please choose a darker foreground or lighter background for reliable scanning."
            }), 400

    px = {"sm": 256, "md": 512, "lg": 1024}.get(size_key, 512)
    box = 10 if px >= 512 else 8

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=box,
        border=4
    )
    qr.add_data(raw)
    qr.make(fit=True)
    img: PilImage = qr.make_image(
        fill_color=fill_color,
        back_color=back_color
    ).convert("RGBA")

    if data_type == "wifi":
        img = _overlay_wifi_png(img, fill_hex=fill_color, back_hex=back_color)
    elif data_type == "vcard" and is_pro():
        img = _overlay_user_png(
            img,
            fill_hex=fill_color,
            back_hex=back_color,
            custom_icon_path=session.get("custom_icon_path")
        )

    img = img.resize((px, px), Image.LANCZOS)

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

    uid = str(uuid.uuid4())

    # --- JPG ---
    jpg_bytes = _save_jpg_from_rgba(
        img,
        quality=(95 if is_one_time() or is_pro() else 92)
    )
    jpg_path = os.path.join(DATA_DIR, f"{uid}.jpg")
    with open(jpg_path, "wb") as f:
        f.write(jpg_bytes)

    # --- SVG (общий, для download_svg) ---
    svg_available = False
    svg_bytes = None
    if is_pro():
        try:
            svg_bytes = _gen_svg_bytes(raw, fill_color, back_color)
            svg_path = os.path.join(DATA_DIR, f"{uid}.svg")
            with open(svg_path, "wb") as f:
                f.write(svg_bytes)
            svg_available = True
        except Exception as e:
            app.logger.error(f"SVG generation failed: {e}")
            svg_bytes = None
            svg_available = False

    # --- ПЕРСИСТЕНТНЫЕ ФАЙЛЫ ДЛЯ DYNAMIC QR ---
    if data_type == "dynamic" and dynamic_id:
        try:
            # JPG под id динамической ссылки
            dyn_jpg_path = os.path.join(DYNAMIC_QR_DIR, f"{dynamic_id}.jpg")
            with open(dyn_jpg_path, "wb") as f_dyn_jpg:
                f_dyn_jpg.write(jpg_bytes)

            # SVG под id динамической ссылки
            if svg_bytes is not None:
                dyn_svg_path = os.path.join(DYNAMIC_QR_DIR, f"{dynamic_id}.svg")
                with open(dyn_svg_path, "wb") as f_dyn_svg:
                    f_dyn_svg.write(svg_bytes)
        except Exception as e:
            app.logger.error(f"Failed to persist dynamic QR files for {dynamic_id}: {e}")

    # --- ответ клиенту ---
    b64 = base64.b64encode(jpg_bytes).decode("utf-8")

    download_name = _build_download_name(data_type, raw)
    session["download_name"] = download_name

    return jsonify({
        "qr_code": b64,
        "id": uid,
        "svg_available": svg_available,
        "dynamic_id": dynamic_id,
        "dynamic_short": dynamic_short,
    })


# ---------------------- DOWNLOADS ----------------------
@app.route("/download_jpg")
def download_jpg():
    file_id = request.args.get("id")
    if not file_id:
        return "Missing id", 400

    # раньше было pop → имя терялось для второго скачивания
    download_name = session.get("download_name", "qrcode")

    path = os.path.join(DATA_DIR, f"{file_id}.jpg")
    if not os.path.exists(path):
        return "Not found", 404
    return send_file(
        path,
        as_attachment=True,
        download_name=f"{download_name}.jpg",
        mimetype="image/jpeg"
    )

@app.route("/dynamic/qr/<id>.jpg")
def dynamic_qr_image(id):
    """
    Постоянная картинка JPG для динамического кода (по его id).
    Открывается в браузере.
    """
    path = os.path.join(DYNAMIC_QR_DIR, f"{id}.jpg")
    if not os.path.exists(path):
        return "Not found", 404
    return send_file(path, mimetype="image/jpeg")


@app.route("/dynamic/qr/<id>.svg")
def dynamic_qr_svg(id):
    """
    SVG для динамического кода (Pro).
    """
    if not is_pro():
        return "Pro required", 403

    path = os.path.join(DYNAMIC_QR_DIR, f"{id}.svg")
    if not os.path.exists(path):
        return "Not found", 404

    return send_file(
        path,
        as_attachment=True,
        download_name=f"dynamic-{id}.svg",
        mimetype="image/svg+xml"
    )




@app.route("/download_svg")
def download_svg():
    if not is_pro():
        return "Pro required", 403
    file_id = request.args.get("id")
    if not file_id:
        return "Missing id", 400

    # тоже get вместо pop
    download_name = session.get("download_name", "qrcode")

    path = os.path.join(DATA_DIR, f"{file_id}.svg")
    if not os.path.exists(path):
        return "Not found", 404

    return send_file(
        path,
        as_attachment=True,
        download_name=f"{download_name}.svg",
        mimetype="image/svg+xml"
    )



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
        db.create_all()
    host = "127.0.0.1"
    port = int(os.environ.get("PORT", 5000))
    print(f"➡  Local server: http://{host}:{port}  (Ctrl+C to stop)")
    app.run(host=host, port=port, debug=True)
