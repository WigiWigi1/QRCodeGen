import os
import json
import uuid
import base64
from io import BytesIO
from datetime import datetime

from flask import (
    Flask, render_template, request, jsonify, send_file,
    send_from_directory, redirect, url_for, session, abort
)
from dotenv import load_dotenv
from authlib.integrations.flask_client import OAuth
from werkzeug.utils import secure_filename

import qrcode
from qrcode.image.pil import PilImage
from qrcode.image.svg import SvgPathImage

from PIL import Image, ImageDraw, ImageFont, ImageFilter


# ---------------------- CONFIG ----------------------
load_dotenv()

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret")

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.getcwd(), "data"))
os.makedirs(DATA_DIR, exist_ok=True)

DYN_PATH = os.path.join(DATA_DIR, "dynamic.json")
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
    client_kwargs={"scope": "openid email profile"},
)

PREMIUM_EMAILS = {
    e.strip().lower() for e in os.environ.get("PREMIUM_EMAILS", "").split(",") if e.strip()
}

ALLOWED_ICON_EXT = {".png"}
MAX_ICON_SIZE_BYTES = 512 * 1024
MAX_ICON_DIM = 512


# ---------------------- HELPERS ----------------------
def current_user():
    return session.get("user")


def is_pro() -> bool:
    u = current_user()
    return bool(u and u.get("email", "").lower() in PREMIUM_EMAILS)


def is_one_time() -> bool:
    return bool(session.get("one_time"))


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
        h = "".join(c*2 for c in h)
    try:
        return tuple(int(h[i:i+2], 16) for i in (0,2,4))
    except Exception:
        return (0,0,0)


def _mix(c_back, c_fill, ratio: float = 0.18):
    rb, gb, bb = c_back
    rf, gf, bf = c_fill
    r = int(rb*(1-ratio) + rf*ratio)
    g = int(gb*(1-ratio) + gf*ratio)
    b = int(bb*(1-ratio) + bf*ratio)
    return (r,g,b)


def _tint_icon_png_to_color(png_rgba: Image.Image, rgb) -> Image.Image:
    icon = png_rgba.convert("RGBA")
    r, g, b, a = icon.split()
    colored = Image.new("RGBA", icon.size, (*rgb, 255))
    colored.putalpha(a)
    return colored


def _draw_badge(base_img: Image.Image, box, radius: int, fill_hex: str, back_hex: str):
    x0,y0,x1,y1 = box
    w = x1-x0; h = y1-y0
    plate_rgb  = _mix(_hex_to_rgb(back_hex), _hex_to_rgb(fill_hex), 0.18)
    outline_rgba = (*_hex_to_rgb(fill_hex), 110)

    # shadow
    shadow_pad = 6
    shadow = Image.new("RGBA", (w+shadow_pad*2, h+shadow_pad*2), (0,0,0,0))
    sdraw = ImageDraw.Draw(shadow)
    sdraw.rounded_rectangle((shadow_pad, shadow_pad, shadow_pad+w, shadow_pad+h),
                            radius=radius, fill=(0,0,0,80))
    shadow = shadow.filter(ImageFilter.GaussianBlur(6))

    plate = Image.new("RGBA", (w, h), (0,0,0,0))
    pdraw = ImageDraw.Draw(plate)
    pdraw.rounded_rectangle((0,0,w,h), radius=radius, fill=(*plate_rgb,255), outline=outline_rgba, width=max(2, w//28))

    inner = Image.new("RGBA", (w, h), (0,0,0,0))
    idraw = ImageDraw.Draw(inner)
    inset = max(1, w//36)
    idraw.rounded_rectangle((inset,inset,w-inset,h-inset), radius=max(1, radius-inset), outline=(255,255,255,90), width=1)

    base = base_img.copy()
    base.alpha_composite(shadow, (x0-shadow_pad, y0-shadow_pad))
    base.alpha_composite(plate, (x0, y0))
    base.alpha_composite(inner, (x0, y0))
    return base


def _overlay_wifi_png(img: Image.Image, fill_hex: str, back_hex: str) -> Image.Image:
    """Wi-Fi иконка (PNG), тонируется под fill."""
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
        target = int(side * 1.1)  # крупнее, как договорились
        colored_png.thumbnail((target, target), Image.LANCZOS)
        iw, ih = colored_png.size
        img.alpha_composite(colored_png, (cx - iw // 2, cy - ih // 2))
    return img


def _overlay_user_png(img: Image.Image, fill_hex: str, back_hex: str, custom_icon_path: str | None) -> Image.Image:
    """vCard: кастомная PNG (если есть и Pro), иначе дефолтный user.png (для Pro)."""
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
    """
    Пытаемся найти и загрузить нормальный TTF-шрифт.
    Возвращаем объект шрифта или None (тогда вызывающий код сделает fallback).
    """
    candidates: list[str] = []

    # 1) Папка проекта (рядом с этим файлом)
    here = os.path.dirname(os.path.abspath(__file__))
    candidates += [
        os.path.join(here, "static", "fonts", "Inter-Medium.ttf"),
        os.path.join(here, "static", "fonts", "DejaVuSans.ttf"),
    ]

    # 2) Папка PIL с шрифтами (обычно есть DejaVuSans.ttf)
    try:
        import PIL
        pil_dir = os.path.dirname(PIL.__file__)
        candidates += [
            os.path.join(pil_dir, "fonts", "DejaVuSans.ttf"),
            os.path.join(pil_dir, "DejaVuSans.ttf"),
        ]
    except Exception:
        pass

    # 3) Системные пути (разные ОС)
    candidates += [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",             # Linux
        "C:\\Windows\\Fonts\\arial.ttf",                               # Windows
        "/System/Library/Fonts/Supplemental/Arial.ttf",                # macOS
        "/Library/Fonts/Arial.ttf",                                    # macOS (альтернатива)
    ]

    # 4) Пробуем по списку
    for p in candidates:
        try:
            if os.path.exists(p):
                return ImageFont.truetype(p, px)
        except Exception:
            continue

    # 5) Последняя попытка по имени
    try:
        return ImageFont.truetype("DejaVuSans.ttf", px)
    except Exception:
        return None


def _add_watermark_border(
    img: Image.Image,
    text: str = "Created by ColorQR.app",
    back_hex: str = "#ffffff",
    fill_hex: str = "#111111",
    font_scale: float = 0.05,   # 10% стороны QR
    margin_scale: float = 0.05, # отступ от QR
    gap_scale: float = 0.05     # промежуток между повторами
) -> Image.Image:
    """
    Повторяющийся текст-бордюр рисуем на 4 отдельных полосах, чтобы ничего
    не попадало внутрь QR. Размер шрифта действительно зависит от font_scale.
    РИСОВАТЬ ПОСЛЕ финального ресайза QR.
    """
    img = img.convert("RGBA")
    W, H = img.size
    side = min(W, H)

    m = max(int(side * margin_scale), 24)  # поля

    back_rgb = _hex_to_rgb(back_hex)
    frame = Image.new("RGBA", (W + 2*m, H + 2*m), (*back_rgb, 255))
    frame.alpha_composite(img, (m, m))

    # --- размер шрифта по scale ---
    fpx = max(int(side * float(font_scale)), 22)

    # --- грузим TTF; если нет — fallback с масштабированием ---
    font = _load_ttf(fpx)
    fallback = False
    if font is None:
        fallback = True
        font = ImageFont.load_default()

    # контраст
    def lum(rgb): r,g,b = [c/255.0 for c in rgb]; return 0.2126*r + 0.7152*g + 0.0722*b
    text_col    = (30,30,30,240) if lum(back_rgb) > 0.6 else (245,245,245,240)
    stroke_col  = (255,255,255,210) if lum(back_rgb) <= 0.6 else (0,0,0,210)
    # при fallback контур чуть толще (маленький битмап масштабируем)
    stroke_w    = max(1, (fpx // 14) if not fallback else (fpx // 10))

    def make_block(rot=0):
        # рисуем в натуральном размере или рисуем мелко и upscale до fpx
        dtmp = ImageDraw.Draw(Image.new("RGBA", (1,1)))
        tw = int(dtmp.textlength(text, font=font))
        th = int((getattr(font, "size", 12)) * 1.2)

        blk = Image.new("RGBA", (tw, th), (0,0,0,0))
        d2  = ImageDraw.Draw(blk)
        d2.text((tw//2, th//2), text, font=font, anchor="mm",
                fill=text_col, stroke_width=stroke_w, stroke_fill=stroke_col)

        if fallback:
            # масштабируем блок так, чтобы высота была ~ fpx*1.2
            target_h = int(fpx * 1.2)
            target_w = max(1, int(blk.width * (target_h / max(1, blk.height))))
            blk = blk.resize((target_w, target_h), Image.LANCZOS)

        if rot:
            blk = blk.rotate(rot, expand=True)
        return blk

    block_h = make_block(0)
    block_v = make_block(90)

    gap_h = max(int((W + 2*m) * float(gap_scale)), int(block_h.width * 0.4))
    gap_v = max(int((H + 2*m) * float(gap_scale)), int(block_v.height * 0.4))

    # горизонтальные полосы
    strip_h_h = max(int(fpx * 1.35), block_h.height + 2)
    def tile_h(y_top):
        strip = Image.new("RGBA", (W + 2*m, strip_h_h), (0,0,0,0))
        x = -block_h.width
        y = strip_h_h//2 - block_h.height//2
        while x < (W + 2*m) - block_h.width + gap_h:
            strip.alpha_composite(block_h, (x, y))
            x += block_h.width + gap_h
        frame.alpha_composite(strip, (0, y_top))

    # вертикальные полосы
    strip_v_w = max(int(fpx * 1.10), block_v.width + 2)
    def tile_v(x_left):
        strip = Image.new("RGBA", (strip_v_w, H + 2*m), (0,0,0,0))
        y = -block_v.height
        x = strip_v_w//2 - block_v.width//2
        while y < (H + 2*m) - block_v.height + gap_v:
            strip.alpha_composite(block_v, (x, y))
            y += block_v.height + gap_v
        frame.alpha_composite(strip, (x_left, 0))

    tile_h(0)           # верх
    tile_h(H + m)       # низ
    tile_v(0)           # лево
    tile_v(W + m)       # право

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
    {"q": "What’s Pro vs One-Time?", "a": "One-Time unlock gives high-quality JPG and extra palettes for a session. Pro adds SVG export, Dynamic QR, vCard with icon and more."},
    {"q": "Do QR codes expire?", "a": "Static codes never expire. Dynamic QR (Pro) can be edited anytime."},
    {"q": "Will a center icon affect scanning?", "a": "We use safe sizes and H error correction. Always test before print."},
    {"q": "Can I change colors?", "a": "Free includes 4 palettes. Paid unlocks 16 extra palettes and custom color."},
]


# ---------------------- ROUTES: PAGES ----------------------
def tpl_args(active):
    return dict(
        active=active,
        user=current_user(),
        is_pro=is_pro(),
        is_one_time=is_one_time(),
        is_paid=is_paid(),
        show_vcard=is_pro(),              # vCard только Pro
        show_dynamic=is_pro(),            # Dynamic QR только Pro
        extra_palettes=is_paid(),         # 16 доп. палитр + кастомный цвет
    )


@app.route("/")
def home():
    return render_template("index.html", **tpl_args("home"))


@app.route("/about")
def about():
    return render_template("about.html", **tpl_args("about"))


@app.route("/pricing")
def pricing():
    return render_template("pricing.html", **tpl_args("pricing"))


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
    redirect_uri = os.environ.get("OAUTH_REDIRECT_URI", url_for("auth_callback", _external=True))
    return oauth.google.authorize_redirect(redirect_uri)


@app.route("/auth/callback")
def auth_callback():
    token = oauth.google.authorize_access_token()
    userinfo = oauth.google.parse_id_token(token)
    session["user"] = {
        "email": userinfo.get("email"),
        "name": userinfo.get("name"),
        "picture": userinfo.get("picture"),
    }
    return redirect(url_for("home"))


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
        f.seek(0); json.dump(data, f); f.truncate()
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
    data_type = (payload.get("data_type") or "url").lower()   # url | wifi | text | vcard | dynamic
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
        img = _overlay_user_png(img, fill_hex=fill_color, back_hex=back_color, custom_icon_path=session.get("custom_icon_path"))

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

    # SVG только для Pro
    if is_pro():
        try:
            svg_bytes = _gen_svg_bytes(raw, fill_color, back_color)
            with open(os.path.join(DATA_DIR, f"{uid}.svg"), "wb") as f:
                f.write(svg_bytes)
        except Exception:
            pass

    b64 = base64.b64encode(jpg_bytes).decode("utf-8")
    return jsonify({"qr_code": b64, "id": uid})


# ---------------------- DOWNLOADS ----------------------
@app.route("/download_jpg")
def download_jpg():
    file_id = request.args.get("id")
    if not file_id:
        return "Missing id", 400
    path = os.path.join(DATA_DIR, f"{file_id}.jpg")
    if not os.path.exists(path):
        return "Not found", 404
    return send_file(path, as_attachment=True, download_name="qrcode.jpg", mimetype="image/jpeg")


@app.route("/download_svg")
def download_svg():
    if not is_pro():
        return "Pro required", 403
    file_id = request.args.get("id")
    path = os.path.join(DATA_DIR, f"{file_id}.svg")
    if not os.path.exists(path):
        return "Not found", 404
    return send_file(path, as_attachment=True, download_name="qrcode.svg", mimetype="image/svg+xml")


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
    host = "127.0.0.1"
    port = int(os.environ.get("PORT", 5000))
    print(f"➡  Local server: http://{host}:{port}  (Ctrl+C to stop)")
    app.run(host=host, port=port, debug=True)
