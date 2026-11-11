"""Public pages: landing, legal, public catalog, product pages, tracked WhatsApp links."""
from flask import (Blueprint, Response, abort, g, redirect, render_template, request,
                   send_from_directory, url_for)
from sqlalchemy import text

import settings as S
from connection import SessionLocal
from core import (Page, active_premium, kes, product_message, record_event,
                  visible_products, wa_link)
from models import Business, Product, User

bp = Blueprint("public", __name__)


@bp.route("/")
def index():
    if g.user:
        return render_template("public/index.html", logged_in=True)
    return render_template("public/index.html", logged_in=False)


@bp.route("/terms")
def terms():
    return render_template("public/terms.html")


@bp.route("/privacy")
def privacy():
    return render_template("public/privacy.html")


@bp.route("/healthz")
def healthz():
    SessionLocal().execute(text("SELECT 1"))
    return {"status": "ok"}


@bp.route("/robots.txt")
def robots():
    body = ("User-agent: *\nDisallow: /admin\nDisallow: /dashboard\nDisallow: /products\n"
            "Disallow: /orders\nDisallow: /account\nDisallow: /go/\n"
            f"Sitemap: {url_for('public.sitemap', _external=True)}\n")
    return Response(body, mimetype="text/plain")


@bp.route("/sitemap.xml")
def sitemap():
    db = SessionLocal()
    rows = (db.query(Business.slug).join(User, Business.user_id == User.id)
            .filter(User.is_active.is_(True)).order_by(Business.id.desc()).limit(5000).all())
    urls = [url_for("public.index", _external=True)] + [url_for("public.shop", slug=r[0], _external=True) for r in rows]
    xml = ('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           + "".join(f"<url><loc>{u}</loc></url>" for u in urls) + "</urlset>")
    return Response(xml, mimetype="application/xml")


@bp.route("/uploads/<path:filename>")
def uploaded_file(filename):
    if not (filename.startswith("products/") or filename.startswith("profiles/")):
        abort(404)
    response = send_from_directory(S.UPLOAD_ROOT, filename)
    response.headers["Cache-Control"] = "public, max-age=604800, immutable"
    return response


# ---------------------------------------------------------------------------
def _load_shop(slug):
    db = SessionLocal()
    business = db.query(Business).filter(Business.slug == slug.lower()).first()
    if business is None or not business.owner.is_active:
        abort(404)
    is_premium = active_premium(db, business.id) is not None
    return db, business, is_premium


def _theme(business, is_premium):
    key = business.theme if is_premium and business.theme in S.THEME_COLORS else "green"
    return S.THEME_COLORS[key]


# Sort options accepted via ?sort=
_SORT_KEYS = ("featured", "newest", "price_asc", "price_desc", "name_asc")


def _apply_filters(products, search, category, in_stock_only, sort_key):
    """Apply ?q / ?category / ?in_stock / ?sort to an already-visible product list.

    Runs in Python because visible_products() applies the free-plan limit()
    before we can order by is_featured / price in SQL. List size is small
    (free = 5, premium = tens–hundreds), so this is fine.
    """
    items = products

    if search:
        needle = search.lower()
        items = [p for p in items
                 if needle in (p.product_name or "").lower()
                 or needle in (p.description or "").lower()]

    if category:
        cat = category.lower()
        items = [p for p in items if (p.category or "").lower() == cat]

    if in_stock_only:
        items = [p for p in items if p.is_available]

    if sort_key == "price_asc":
        items.sort(key=lambda p: (not p.is_available, float(p.price or 0), -p.id))
    elif sort_key == "price_desc":
        items.sort(key=lambda p: (not p.is_available, -float(p.price or 0), -p.id))
    elif sort_key == "name_asc":
        items.sort(key=lambda p: (not p.is_available, (p.product_name or "").lower()))
    elif sort_key == "newest":
        items.sort(key=lambda p: (not p.is_available, -p.id))
    else:  # "featured" (default): featured first, then newest
        items.sort(key=lambda p: (not p.is_available, not p.is_featured, -p.id))

    return items


@bp.route("/shop/<slug>")
def shop(slug):
    db, business, is_premium = _load_shop(slug)

    # 1. All visible products (respects the free-plan limit)
    all_products = visible_products(db, business, is_premium)

    # 2. Read filter args (all optional, all sanitised)
    search = (request.args.get("q") or "").strip()[:80]
    category = (request.args.get("category") or "").strip()[:80]
    sort_key = request.args.get("sort", "featured")
    if sort_key not in _SORT_KEYS:
        sort_key = "featured"
    in_stock_only = request.args.get("in_stock") in ("1", "true", "on")

    # 3. Filter + sort (in Python, on the already-visible list)
    filtered = _apply_filters(all_products, search, category, in_stock_only, sort_key)

    for p in filtered:
        p.go_url = url_for("public.order_go", slug=business.slug, product_id=p.id)

    # 4. Pagination (manual slice, since we sorted in Python)
    per_page = S.CATALOG_PAGE_SIZE
    total = len(filtered)
    try:
        page_no = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page_no = 1
    pages = max(1, -(-total // per_page)) if total else 1
    page_no = min(page_no, pages)
    start = (page_no - 1) * per_page
    page_products = filtered[start:start + per_page]
    page = Page(page_products, total, page_no, per_page)

    # 5. Category pills use the *unfiltered* visible set so the choices never
    #    disappear based on what is currently filtered.
    categories = sorted({p.category for p in all_products if p.category})

    record_event(db, business, "view")
    return render_template(
        "public/catalog.html",
        business=business,
        products=page_products,
        page=page,
        categories=categories,
        search=search,
        active_category=category,
        active_sort=sort_key,
        in_stock_only=in_stock_only,
        theme_color=_theme(business, is_premium),
        branding=not is_premium,
        can_chat=bool(business.whatsapp_number),
        chat_url=url_for("public.chat_go", slug=business.slug),
        share_url=url_for("public.shop", slug=business.slug, _external=True),
    )


@bp.route("/shop/<slug>/p/<int:product_id>")
def product_page(slug, product_id):
    db, business, is_premium = _load_shop(slug)
    visible = visible_products(db, business, is_premium)
    product = next((p for p in visible if p.id == product_id), None)
    if product is None:
        abort(404)
    related = [p for p in visible if p.id != product.id and product.category and p.category == product.category][:4]
    for p in related + [product]:
        p.go_url = url_for("public.order_go", slug=business.slug, product_id=p.id)
    record_event(db, business, "product_view", product.id)
    return render_template(
        "public/product.html", business=business, product=product, related=related,
        theme_color=_theme(business, is_premium), branding=not is_premium,
        can_chat=bool(business.whatsapp_number),
        share_url=url_for("public.product_page", slug=business.slug, product_id=product.id, _external=True),
    )


@bp.route("/go/<slug>/<int:product_id>")
def order_go(slug, product_id):
    """Counts the click, then forwards the customer to a pre-filled WhatsApp message."""
    db, business, is_premium = _load_shop(slug)
    product = next((p for p in visible_products(db, business, is_premium) if p.id == product_id), None)
    if product is None or not business.whatsapp_number:
        abort(404)
    if not product.is_available:
        return redirect(url_for("public.product_page", slug=slug, product_id=product_id))
    record_event(db, business, "order_click", product.id)
    return redirect(wa_link(business.whatsapp_number, product_message(business, product)), code=302)


@bp.route("/go/<slug>")
def chat_go(slug):
    db, business, _ = _load_shop(slug)
    if not business.whatsapp_number:
        abort(404)
    record_event(db, business, "chat_click")
    msg = f"Hello {business.business_name}, I saw your catalog and would like to ask about your products."
    return redirect(wa_link(business.whatsapp_number, msg), code=302)