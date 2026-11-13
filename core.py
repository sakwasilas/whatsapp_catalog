"""Shared helpers: formatting, uploads, tokens, mail, throttling, plan rules, analytics."""
import hashlib
import io
import logging
import os
import re
import secrets
import smtplib
import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from functools import wraps
from urllib.parse import quote, urlparse

from flask import abort, current_app, flash, g, redirect, request, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import func

import settings as S
from connection import SessionLocal
from models import (AdminLog, Business, Event, Order, Payment, Product, RateEvent,
                    Subscription, User)

log = logging.getLogger("catalog")
Image.MAX_IMAGE_PIXELS = 40_000_000   # refuse decompression bombs


# --------------------------------------------------------------------------
# Time & formatting
# --------------------------------------------------------------------------
def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_eat(dt):
    return dt + timedelta(hours=3) if dt else None


def kes(value):
    try:
        value = Decimal(value)
    except (InvalidOperation, TypeError):
        return "KSh 0"
    if value == value.to_integral_value():
        return "KSh {:,}".format(int(value))
    return "KSh {:,.2f}".format(value)


def fmt_date(dt):
    return to_eat(dt).strftime("%d %b %Y") if dt else "-"


def fmt_dt(dt):
    return to_eat(dt).strftime("%d %b %Y, %H:%M") if dt else "-"


def timeago(dt):
    if not dt:
        return "never"
    secs = int((utcnow() - dt).total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60} min ago"
    if secs < 86400:
        return f"{secs // 3600} h ago"
    if secs < 86400 * 30:
        return f"{secs // 86400} d ago"
    return fmt_date(dt)


def register_template_helpers(app):
    app.template_filter("kes")(kes)
    app.template_filter("fmt_date")(fmt_date)
    app.template_filter("fmt_dt")(fmt_dt)
    app.template_filter("timeago")(timeago)
    app.jinja_env.globals["page_url"] = page_url          # NEW: pagination URL helper

    def upload_url(path, default=None):
        return url_for("public.uploaded_file", filename=path) if path else default

    app.jinja_env.globals["upload_url"] = upload_url


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MPESA_CODE_RE = re.compile(r"^[A-Z0-9]{10}$")


def normalize_phone(raw):
    """Digits in international format (254712345678) or None."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 10:
        digits = "254" + digits[1:]
    elif len(digits) == 9 and digits[0] in "71":
        digits = "254" + digits
    return digits if re.fullmatch(r"\d{10,15}", digits) else None


def slugify(text_value):
    slug = re.sub(r"[^a-z0-9]+", "-", (text_value or "").lower()).strip("-")
    return slug[:60].strip("-") or "shop"


def unique_slug(db, base):
    slug, n = base, 2
    while db.query(Business.id).filter(Business.slug == slug).first():
        slug = f"{base}-{n}"
        n += 1
    return slug


def parse_money(raw, allow_empty=False):
    """Return (Decimal|None, ok)."""
    raw = (raw or "").strip().replace(",", "")
    if not raw:
        return None, allow_empty
    try:
        value = Decimal(raw)
        if value < 0 or value > Decimal("10000000") or value != value.quantize(Decimal("0.01")):
            return None, False
        return value, True
    except InvalidOperation:
        return None, False


def safe_referrer(default):
    ref = request.referrer
    if ref and urlparse(ref).netloc == request.host:
        return ref
    return default


def wa_link(number, message):
    return f"https://wa.me/{number}?text={quote(message, safe='')}"


def product_message(business, product):
    return (f"Hello {business.business_name}, I am interested in ordering:\n\n"
            f"Product: {product.product_name}\nPrice: {kes(product.price)}\n\n"
            "Is this product available?")


# --------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------
class Page:
    """One page of results, with metadata for rendering pagination controls."""

    def __init__(self, items, total, page, per_page):
        self.items = items
        self.total = total
        self.per_page = max(1, int(per_page or 1))
        self.pages = max(1, -(-total // self.per_page)) if total else 1
        self.page = min(max(1, int(page or 1)), self.pages)
        self.has_prev = self.page > 1
        self.has_next = self.page < self.pages
        self.prev_num = self.page - 1 if self.has_prev else None
        self.next_num = self.page + 1 if self.has_next else None

    def iter_pages(self, edge=1, around=2):
        """Yield page numbers; None marks a gap (e.g. 1, 2, None, 8, 9, None, 20)."""
        last = 0
        for n in range(1, self.pages + 1):
            if (n <= edge
                    or n > self.pages - edge
                    or (self.page - around) <= n <= (self.page + around)):
                if last + 1 != n:
                    yield None
                yield n
                last = n


def paginate(query, per_page=20, max_per_page=60):
    """Paginate a SQLAlchemy query using ?page=N from the request.

    Clamps bad input (non-int, <1, per_page out of range). Returns a Page.
    NOTE: `.count()` is called on the query as-is; keep any needed filters on
    the query you pass in. ORDER BY is stripped for the count for portability.
    """
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    per_page = max(1, min(int(per_page or 20), max_per_page))
    total = query.order_by(None).count() if hasattr(query, "order_by") else 0
    items = query.limit(per_page).offset((page - 1) * per_page).all()
    return Page(items, total, page, per_page)


def page_url(page_no, **overrides):
    """URL for the current endpoint with page=N, preserving existing query args."""
    args = {**request.args.to_dict(flat=True), "page": page_no, **overrides}
    return url_for(request.endpoint, **(request.view_args or {}), **args)


# --------------------------------------------------------------------------
# Images: validate, strip metadata, resize, store as WebP under a random name
# --------------------------------------------------------------------------
def _detect_image_type(header):
    if header[:3] == b"\xff\xd8\xff":
        return "jpg"
    if header[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "webp"
    return None


def save_image(file_storage, subfolder):
    """Returns (relative_path, error). (None, None) means no file was supplied."""
    if file_storage is None or not file_storage.filename:
        return None, None
    name = file_storage.filename
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in S.ALLOWED_EXTENSIONS:
        return None, "Only JPG, PNG and WEBP images are allowed."

    data = file_storage.stream.read(S.MAX_IMAGE_BYTES + 1)
    if not data:
        return None, "The uploaded image is empty."
    if len(data) > S.MAX_IMAGE_BYTES:
        return None, "Image is too large. Maximum size is 4 MB."
    detected = _detect_image_type(data[:12])
    if detected is None:
        return None, "That file is not a valid JPG, PNG or WEBP image."
    if (ext in ("jpg", "jpeg") and detected != "jpg") or (ext in ("png", "webp") and ext != detected):
        return None, "The file content does not match its extension."

    max_px = S.LOGO_MAX_PX if subfolder == "profiles" else S.PRODUCT_IMAGE_MAX_PX
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        img = ImageOps.exif_transpose(img)
        img.thumbnail((max_px, max_px), Image.LANCZOS)
        has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
        img = img.convert("RGBA" if has_alpha else "RGB")
        filename = f"{uuid.uuid4().hex}.webp"
        folder = S.PROFILE_UPLOAD_DIR if subfolder == "profiles" else S.PRODUCT_UPLOAD_DIR
        os.makedirs(folder, exist_ok=True)
        img.save(os.path.join(folder, filename), "WEBP", quality=82, method=4)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError):
        return None, "That image could not be read. Try another photo."
    return f"{subfolder}/{filename}", None


def delete_upload(rel_path):
    if not rel_path:
        return
    full = os.path.realpath(os.path.join(S.UPLOAD_ROOT, rel_path))
    root = os.path.realpath(S.UPLOAD_ROOT)
    if os.path.commonpath([root, full]) == root and os.path.isfile(full):
        try:
            os.remove(full)
        except OSError:
            pass


# --------------------------------------------------------------------------
# Signed tokens (password reset / email verification)
# --------------------------------------------------------------------------
def _serializer(purpose):
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=f"catalog-{purpose}")


def _fingerprint(user):
    # Changes whenever the password changes, so a reset link works exactly once.
    return hashlib.sha256((user.password_hash or "").encode()).hexdigest()[:16]


def make_reset_token(user):
    return _serializer("reset").dumps({"uid": user.id, "fp": _fingerprint(user)})


def read_reset_token(db, token):
    try:
        data = _serializer("reset").loads(token, max_age=S.RESET_TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    user = db.get(User, data.get("uid"))
    if user and user.is_active and data.get("fp") == _fingerprint(user):
        return user
    return None


def make_verify_token(user):
    return _serializer("verify").dumps({"uid": user.id, "email": user.email})


def read_verify_token(db, token):
    try:
        data = _serializer("verify").loads(token, max_age=S.VERIFY_TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    user = db.get(User, data.get("uid"))
    if user and user.email == data.get("email"):
        return user
    return None


# --------------------------------------------------------------------------
# Email (SMTP in a background thread; logs + OUTBOX when SMTP is not configured)
# --------------------------------------------------------------------------
OUTBOX = []      # inspected by tests / handy in development


def _smtp_send(msg):
    try:
        cls = smtplib.SMTP_SSL if S.MAIL_USE_SSL else smtplib.SMTP
        with cls(S.MAIL_HOST, S.MAIL_PORT, timeout=15) as smtp:
            if not S.MAIL_USE_SSL:
                try:
                    smtp.starttls()
                except smtplib.SMTPException:
                    pass
            if S.MAIL_USERNAME:
                smtp.login(S.MAIL_USERNAME, S.MAIL_PASSWORD)
            smtp.send_message(msg)
    except Exception:           # never let mail problems break a request
        log.exception("Could not send email to %s", msg["To"])


def send_mail(to, subject, body):
    if S.MAIL_ENABLED:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = S.MAIL_FROM, to, subject
        msg.set_content(body)
        threading.Thread(target=_smtp_send, args=(msg,), daemon=True).start()
    else:
        OUTBOX.append({"to": to, "subject": subject, "body": body})
        del OUTBOX[:-50]
        log.warning("[mail not configured] To: %s | %s\n%s", to, subject, body)



def send_verification_email(user):
    token = make_verify_token(user)

    base_url = S.PUBLIC_BASE_URL.rstrip("/")

    link = f"{base_url}{url_for('auth.verify_email', token=token)}"

    send_mail(
        user.email,
        f"Confirm your email - {S.PLATFORM_NAME}",
        f"Hi {user.full_name},\n\n"
        f"Confirm your email address by opening this link "
        f"(valid for 3 days):\n{link}\n\n"
        f"If you did not create an account, ignore this email."
    )



def send_login_otp_email(user, code):
    send_mail(user.email, f"Your Duka Yangu sign-in code",
              f"Hi {user.full_name},\n\nYour Duka Yangu sign-in code is: {code}\n\n"
              "It expires in 5 minutes. If you did not try to sign in, you can ignore this email.")


def send_reset_email(user):
    link = url_for("auth.reset_password", token=make_reset_token(user), _external=True)
    send_mail(user.email, f"Reset your password - {S.PLATFORM_NAME}",
              f"Hi {user.full_name},\n\nUse this link to choose a new password "
              f"(valid for 1 hour, works once):\n{link}\n\n"
              "If you did not ask for this, you can ignore this email - your password is unchanged.")


# --------------------------------------------------------------------------
# Throttling (stored in the database so it works across workers and restarts)
# --------------------------------------------------------------------------
def client_ip():
    return request.remote_addr or "0.0.0.0"


def rate_count(db, bucket, window_seconds):
    since = utcnow() - timedelta(seconds=window_seconds)
    return db.query(func.count(RateEvent.id)).filter(
        RateEvent.bucket == bucket, RateEvent.created_at >= since).scalar() or 0


def rate_hit(db, bucket):
    db.add(RateEvent(bucket=bucket))
    db.commit()


def rate_clear(db, bucket):
    db.query(RateEvent).filter(RateEvent.bucket == bucket).delete()
    db.commit()


# --------------------------------------------------------------------------
# Authorization decorators
# --------------------------------------------------------------------------
def seller_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.user is None:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("auth.login", next=request.full_path if request.method == "GET" else None))
        if g.user.role == "admin":
            return redirect(url_for("admin.dashboard"))
        return view(*args, **kwargs)
    return wrapper


def business_required(view):
    @wraps(view)
    @seller_required
    def wrapper(*args, **kwargs):
        business = SessionLocal().query(Business).filter(Business.user_id == g.user.id).first()
        if business is None:
            flash("Set up your business profile first.", "info")
            return redirect(url_for("seller.business_profile"))
        g.business = business
        return view(*args, **kwargs)
    return wrapper


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("auth.admin_login"))
        if g.user.role != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapper


def safe_next(default):
    nxt = request.values.get("next", "")
    if nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt:
        return nxt.rstrip("?")
    return default


# --------------------------------------------------------------------------
# Plan rules (premium is always derived from the database, never from the browser)
# --------------------------------------------------------------------------
def active_premium(db, business_id):
    return (db.query(Subscription)
            .filter(Subscription.business_id == business_id, Subscription.plan_type == "premium",
                    Subscription.status == "active", Subscription.expiry_date > utcnow())
            .order_by(Subscription.expiry_date.desc()).first())


def log_admin(db, admin_id, action, description):
    db.add(AdminLog(admin_id=admin_id, action=action, description=description))


def grant_premium(db, business, days, admin_id, source, action, detail):
    """The single place premium is granted (payment approval, manual grant, future Daraja callback)."""
    now = utcnow()
    current = active_premium(db, business.id)
    if current:
        current.expiry_date = current.expiry_date + timedelta(days=days)
        expiry = current.expiry_date
    else:
        expiry = now + timedelta(days=days)
        db.add(Subscription(business_id=business.id, plan_type="premium", status="active",
                            source=source, start_date=now, expiry_date=expiry))
    log_admin(db, admin_id, action, f"{detail} Premium until {expiry:%Y-%m-%d}.")
    return expiry


def revoke_premium(db, business, admin_id):
    rows = (db.query(Subscription)
            .filter(Subscription.business_id == business.id, Subscription.plan_type == "premium",
                    Subscription.status == "active", Subscription.expiry_date > utcnow()).all())
    for row in rows:
        row.status = "expired"
        row.expiry_date = utcnow()
    log_admin(db, admin_id, "revoke_premium", f"Premium revoked for business #{business.id}.")
    return len(rows)


def business_stats(db, business):
    total = db.query(func.count(Product.id)).filter(Product.business_id == business.id).scalar() or 0
    premium = active_premium(db, business.id)
    is_premium = premium is not None
    had_premium = db.query(Subscription.id).filter(
        Subscription.business_id == business.id, Subscription.plan_type == "premium").first() is not None
    used = total if is_premium else min(total, S.FREE_PRODUCT_LIMIT)
    return {
        "total": total, "is_premium": is_premium, "limit": S.FREE_PRODUCT_LIMIT, "used": used,
        "remaining": None if is_premium else max(0, S.FREE_PRODUCT_LIMIT - total),
        "percent": 100 if is_premium else min(100, int(used * 100 / S.FREE_PRODUCT_LIMIT)),
        "hidden": 0 if is_premium else max(0, total - S.FREE_PRODUCT_LIMIT),
        "expiry": premium.expiry_date if premium else None,
        "days_left": max(0, (premium.expiry_date - utcnow()).days) if premium else None,
        "expired_notice": had_premium and not is_premium,
        "can_add": is_premium or total < S.FREE_PRODUCT_LIMIT,
        "analytics_days": S.PREMIUM_ANALYTICS_DAYS if is_premium else S.FREE_ANALYTICS_DAYS,
    }


def visible_products(db, business, is_premium):
    q = db.query(Product).filter(Product.business_id == business.id).order_by(Product.id.asc())
    if not is_premium:
        q = q.limit(S.FREE_PRODUCT_LIMIT)
    return q.all()


# --------------------------------------------------------------------------
# Analytics
# --------------------------------------------------------------------------
_BOT_RE = re.compile(r"bot|crawl|spider|slurp|preview|facebookexternalhit|whatsapp|curl|wget|python|monitor|headless",
                     re.I)


def is_bot():
    ua = request.headers.get("User-Agent", "")
    return not ua or bool(_BOT_RE.search(ua))


def _visitor_hash():
    raw = f"{current_app.config['SECRET_KEY']}|{client_ip()}|{request.headers.get('User-Agent', '')}|{utcnow():%Y-%m-%d}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def record_event(db, business, event_type, product_id=None):
    """Count a public interaction. Skips bots and the shop owner's own visits."""
    if is_bot() or (g.get("user") is not None and g.user.id == business.user_id):
        return
    try:
        db.add(Event(business_id=business.id, product_id=product_id,
                     event_type=event_type, visitor=_visitor_hash()))
        db.commit()
    except Exception:
        db.rollback()
        log.exception("Could not record event")


def analytics_summary(db, business_id, days, with_top_products=False):
    now = utcnow()
    since = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    base = db.query(Event).filter(Event.business_id == business_id, Event.created_at >= since)

    counts = dict(base.with_entities(Event.event_type, func.count(Event.id)).group_by(Event.event_type).all())
    visitors = base.filter(Event.event_type == "view").with_entities(
        func.count(func.distinct(Event.visitor))).scalar() or 0
    views = counts.get("view", 0)
    clicks = counts.get("order_click", 0) + counts.get("chat_click", 0)

    by_day = {}
    for day, etype, n in (base.with_entities(func.date(Event.created_at), Event.event_type, func.count(Event.id))
                          .group_by(func.date(Event.created_at), Event.event_type).all()):
        by_day.setdefault(str(day), {})[etype] = n
    series = []
    for i in range(days):
        d = (since + timedelta(days=i)).date()
        row = by_day.get(d.isoformat(), {})
        series.append({"date": d, "label": d.strftime("%d %b"), "views": row.get("view", 0),
                       "clicks": row.get("order_click", 0) + row.get("chat_click", 0)})
    peak = max([s["views"] for s in series] + [1])
    for s in series:
        s["h"] = int(s["views"] * 100 / peak)
        s["hc"] = int(s["clicks"] * 100 / peak)

    result = {
        "days": days, "views": views, "visitors": visitors, "clicks": clicks,
        "product_views": counts.get("product_view", 0),
        "rate": round(clicks * 100 / visitors) if visitors else 0,
        "series": series, "top": [],
    }
    if with_top_products:
        rows = (base.filter(Event.product_id.isnot(None))
                .with_entities(Event.product_id, Event.event_type, func.count(Event.id))
                .group_by(Event.product_id, Event.event_type).all())
        agg = {}
        for pid, etype, n in rows:
            agg.setdefault(pid, {"views": 0, "clicks": 0})
            agg[pid]["clicks" if etype == "order_click" else "views"] += n
        names = {p.id: p for p in db.query(Product).filter(Product.id.in_(list(agg) or [0]))}
        top = [{"product": names[pid], **v} for pid, v in agg.items() if pid in names]
        top.sort(key=lambda r: (r["clicks"], r["views"]), reverse=True)
        result["top"] = top[:5]
    return result


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------
def order_summary(db, business_id):
    rows = dict(db.query(Order.status, func.count(Order.id))
                .filter(Order.business_id == business_id).group_by(Order.status).all())
    month_start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    revenue = (db.query(func.coalesce(func.sum(Order.total_amount), 0))
               .filter(Order.business_id == business_id, Order.status == "delivered",
                       Order.created_at >= month_start).scalar() or 0)
    return {"counts": {s: rows.get(s, 0) for s in S.ORDER_STATUSES},
            "open": rows.get("new", 0) + rows.get("confirmed", 0),
            "total": sum(rows.values()), "month_revenue": revenue}