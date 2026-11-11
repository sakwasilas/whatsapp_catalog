"""Seller area: dashboard, business profile, products, orders, analytics, subscription, account."""
import csv
import io
import os
import re
import secrets
import shutil
import uuid
from decimal import Decimal
from urllib.parse import quote

import segno
from flask import (Blueprint, Response, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

import settings as S
from connection import SessionLocal
from core import (EMAIL_RE, MPESA_CODE_RE, active_premium, analytics_summary, business_stats,
                  business_required, delete_upload, kes, normalize_phone, order_summary, page_url,
                  paginate, parse_money, safe_referrer, save_image, send_verification_email,
                  seller_required, slugify, unique_slug, utcnow, wa_link)
from models import Business, Category, Event, Order, Payment, Product, Subscription, User

bp = Blueprint("seller", __name__)
bp.add_app_template_global(page_url, "page_url")


# ============================ dashboard =====================================
@bp.route("/dashboard")
@business_required
def dashboard():
    db, business = SessionLocal(), g.business
    stats = business_stats(db, business)
    catalog_url = url_for("public.shop", slug=business.slug, _external=True)
    wa_share_url = "https://wa.me/?text=" + quote(
        f"Check out {business.business_name} - browse and order on WhatsApp: {catalog_url}", safe="")
    days = min(stats["analytics_days"], 14)
    stats_week = analytics_summary(db, business.id, days)
    orders = order_summary(db, business.id)
    recent_orders = (db.query(Order).filter(Order.business_id == business.id)
                     .order_by(Order.id.desc()).limit(5).all())
    recent_products = (db.query(Product).filter(Product.business_id == business.id)
                       .order_by(Product.id.desc()).limit(4).all())
    ever_viewed = db.query(Event.id).filter(Event.business_id == business.id, Event.event_type == "view").first()
    checklist = [
        ("Create your account", True, None),
        ("Set up your business profile", True, None),
        ("Add your logo", bool(business.logo), url_for("seller.business_profile")),
        ("Add your first product", stats["total"] >= 1, url_for("seller.add_product")),
        ("Add 3 or more products", stats["total"] >= 3, url_for("seller.add_product")),
        ("Share your link and get a visitor", ever_viewed is not None, None),
    ]
    if S.MAIL_ENABLED:
        checklist.append(("Confirm your email", g.user.email_verified, None))
    done = sum(1 for c in checklist if c[1])
    return render_template(
        "seller/dashboard.html", business=business, stats=stats, catalog_url=catalog_url,
        wa_share_url=wa_share_url, recent_products=recent_products, week=stats_week,
        orders=orders, recent_orders=recent_orders, checklist=checklist,
        progress=int(done * 100 / len(checklist)), setup_complete=done == len(checklist))


@bp.route("/dashboard/qr.<fmt>")
@business_required
def qr_code(fmt):
    if fmt not in ("svg", "png"):
        abort(404)
    url = url_for("public.shop", slug=g.business.slug, _external=True)
    qr = segno.make(url, error="m")
    buf = io.BytesIO()
    if fmt == "svg":
        qr.save(buf, kind="svg", scale=10, border=2, dark="#075e54")
        mime = "image/svg+xml"
    else:
        qr.save(buf, kind="png", scale=14, border=3)
        mime = "image/png"
    resp = Response(buf.getvalue(), mimetype=mime)
    if request.args.get("download"):
        resp.headers["Content-Disposition"] = f'attachment; filename="{g.business.slug}-qr.{fmt}"'
    return resp


# ============================ analytics =====================================
@bp.route("/analytics")
@business_required
def analytics():
    db, business = SessionLocal(), g.business
    stats = business_stats(db, business)
    allowed = [7, 30, 90] if stats["is_premium"] else [7]
    try:
        days = int(request.args.get("days", 7))
    except ValueError:
        days = 7
    if days not in allowed:
        days = 7
    data = analytics_summary(db, business.id, days, with_top_products=stats["is_premium"])
    return render_template("seller/analytics.html", business=business, stats=stats, data=data,
                           days=days, allowed=allowed)


# ============================ business profile ==============================
@bp.route("/business/profile", methods=["GET", "POST"])
@seller_required
def business_profile():
    db = SessionLocal()
    business = db.query(Business).filter(Business.user_id == g.user.id).first()
    is_premium = bool(business and active_premium(db, business.id))

    def render(form):
        return render_template("seller/business_profile.html", business=business, form=form,
                               business_categories=S.BUSINESS_CATEGORIES, is_premium=is_premium)

    form = {}
    if business:
        form = {"business_name": business.business_name, "whatsapp_number": business.whatsapp_number or "",
                "location": business.location or "", "description": business.description or "",
                "business_category": business.business_category or "",
                "instagram": business.instagram or "", "theme": business.theme or "green"}
    if request.method != "POST":
        return render(form)

    form = {k: request.form.get(k, "").strip() for k in
            ("business_name", "whatsapp_number", "location", "description", "business_category", "instagram", "theme")}
    errors = []
    if len(form["business_name"]) < 2 or len(form["business_name"]) > 150:
        errors.append("Business name must be between 2 and 150 characters.")
    wa = normalize_phone(form["whatsapp_number"])
    if not wa:
        errors.append("Please enter a valid WhatsApp number, e.g. 0712 345 678.")
    if len(form["location"]) > 150:
        errors.append("Location is too long (max 150 characters).")
    if len(form["description"]) > 1000:
        errors.append("Description is too long (max 1000 characters).")
    if form["business_category"] not in S.BUSINESS_CATEGORIES:
        errors.append("Please choose a business category.")
    insta = form["instagram"].lstrip("@")
    if insta and not re.fullmatch(r"[A-Za-z0-9._]{1,30}", insta):
        errors.append("Instagram handle can only contain letters, numbers, dots and underscores.")
    theme = form["theme"] if form["theme"] in S.THEME_COLORS else "green"
    if not is_premium:
        theme = "green"

    logo_path, logo_error = save_image(request.files.get("logo"), "profiles")
    if logo_error:
        errors.append(logo_error)
    if errors:
        delete_upload(logo_path)
        for e in errors:
            flash(e, "danger")
        return render(form)

    creating = business is None
    old_logo = None
    if creating:
        business = Business(user_id=g.user.id)
        db.add(business)
    business.business_name = form["business_name"]
    business.whatsapp_number = wa
    business.location = form["location"] or None
    business.description = form["description"] or None
    business.business_category = form["business_category"]
    business.instagram = insta or None
    business.theme = theme
    if logo_path:
        old_logo, business.logo = business.logo, logo_path
    elif request.form.get("remove_logo") and business.logo:
        old_logo, business.logo = business.logo, None

    saved = False
    for attempt in range(5):
        try:
            if creating:
                base = slugify(form["business_name"])    # generated once; links never change
                business.slug = unique_slug(db, base if attempt == 0 else f"{base}-{secrets.token_hex(2)}")
                db.add(business)
                db.flush()
                db.add(Subscription(business_id=business.id, plan_type="free", status="active"))
            db.commit()
            saved = True
            break
        except IntegrityError:
            db.rollback()
            if not creating:
                break
    if not saved:
        delete_upload(logo_path)
        flash("Could not save your business. Please try again.", "danger")
        business = None if creating else business
        return render(form)
    delete_upload(old_logo)
    flash("Business profile saved.", "success")
    return redirect(url_for("seller.dashboard") if creating else url_for("seller.business_profile"))


# ============================ products ======================================
def parse_product_form(is_premium_sale=True):
    f = request.form
    form = {"product_name": f.get("product_name", "").strip(), "description": f.get("description", "").strip(),
            "price": f.get("price", "").strip(), "compare_price": f.get("compare_price", "").strip(),
            "category": f.get("category", "").strip(), "is_available": f.get("is_available") == "on",
            "is_featured": f.get("is_featured") == "on"}
    errors = []
    if not 2 <= len(form["product_name"]) <= 150:
        errors.append("Product name must be between 2 and 150 characters.")
    if len(form["description"]) > 2000:
        errors.append("Description is too long (max 2000 characters).")
    if len(form["category"]) > 80:
        errors.append("Category is too long (max 80 characters).")
    price, ok = parse_money(form["price"])
    if not ok or price is None:
        errors.append("Enter a valid price in KSh (for example 1500 or 1500.50).")
    compare, ok = parse_money(form["compare_price"], allow_empty=True)
    if not ok:
        errors.append("Enter a valid old price, or leave it empty.")
    elif compare is not None and price is not None and compare <= price:
        errors.append("The old price must be higher than the current price.")
    return form, price, compare, errors


def category_suggestions(db, business):
    saved = [c.category_name for c in db.query(Category).filter(Category.business_id == business.id)]
    return sorted(set(saved) | set(S.DEFAULT_PRODUCT_CATEGORIES))


def remember_category(db, business, name):
    if name and not db.query(Category.id).filter(Category.business_id == business.id,
                                                 Category.category_name == name).first():
        db.add(Category(business_id=business.id, category_name=name))


def get_own_product_or_404(db, business, product_id):
    product = db.query(Product).filter(Product.id == product_id, Product.business_id == business.id).first()
    if product is None:
        abort(404)
    return product


@bp.route("/products")
@business_required
def products():
    db, business = SessionLocal(), g.business
    stats = business_stats(db, business)
    q = (request.args.get("q") or "").strip()
    query = db.query(Product).filter(Product.business_id == business.id)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Product.product_name.like(like), Product.category.like(like)))
    items = query.order_by(Product.id.asc()).all()
    all_ids = [r[0] for r in db.query(Product.id).filter(Product.business_id == business.id)
               .order_by(Product.id.asc()).all()]
    visible_ids = None if stats["is_premium"] else set(all_ids[:S.FREE_PRODUCT_LIMIT])
    return render_template("seller/products.html", business=business, products=items, stats=stats,
                           visible_ids=visible_ids, q=q)


@bp.route("/products/add", methods=["GET", "POST"])
@business_required
def add_product():
    db, business = SessionLocal(), g.business
    suggestions = category_suggestions(db, business)
    stats = business_stats(db, business)
    form = {"is_available": True}

    def limit_flash():
        flash(f"You have reached your free product limit. Upgrade for only KSh {S.PREMIUM_PRICE} "
              f"and showcase unlimited products for {S.PREMIUM_DAYS} days.", "warning")

    if request.method == "POST":
        # Lock the business row so simultaneous requests cannot both slip past the limit.
        # (business_stats performs no commits, so the lock is held until our commit.)
        db.query(Business).filter(Business.id == business.id).with_for_update().one()
        stats = business_stats(db, business)
        if not stats["can_add"]:
            db.rollback()
            limit_flash()
            return redirect(url_for("seller.subscription"))
        form, price, compare, errors = parse_product_form()
        image_path, image_error = save_image(request.files.get("image"), "products")
        if image_error:
            errors.append(image_error)
        if errors:
            db.rollback()
            delete_upload(image_path)
            for e in errors:
                flash(e, "danger")
            return render_template("seller/product_form.html", business=business, stats=stats, form=form,
                                   suggestions=suggestions, product=None)
        db.add(Product(business_id=business.id, product_name=form["product_name"],
                       description=form["description"] or None, price=price, compare_price=compare,
                       image=image_path, category=form["category"] or None,
                       is_available=form["is_available"], is_featured=form["is_featured"]))
        remember_category(db, business, form["category"])
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            delete_upload(image_path)
            flash("Could not save the product. Please try again.", "danger")
            return render_template("seller/product_form.html", business=business, stats=stats, form=form,
                                   suggestions=suggestions, product=None)
        flash("Product added.", "success")
        return redirect(url_for("seller.products"))

    if not stats["can_add"]:
        limit_flash()
    return render_template("seller/product_form.html", business=business, stats=stats, form=form,
                           suggestions=suggestions, product=None)


@bp.route("/products/edit/<int:product_id>", methods=["GET", "POST"])
@business_required
def edit_product(product_id):
    db, business = SessionLocal(), g.business
    product = get_own_product_or_404(db, business, product_id)
    suggestions = category_suggestions(db, business)
    stats = business_stats(db, business)

    def fmt(v):
        return "{:f}".format(v.normalize()) if v is not None else ""

    form = {"product_name": product.product_name, "description": product.description or "",
            "price": fmt(product.price), "compare_price": fmt(product.compare_price),
            "category": product.category or "", "is_available": product.is_available,
            "is_featured": product.is_featured}
    if request.method == "POST":
        form, price, compare, errors = parse_product_form()
        image_path, image_error = save_image(request.files.get("image"), "products")
        if image_error:
            errors.append(image_error)
        if errors:
            delete_upload(image_path)
            for e in errors:
                flash(e, "danger")
            return render_template("seller/product_form.html", business=business, stats=stats, form=form,
                                   suggestions=suggestions, product=product)
        old_image = None
        product.product_name, product.description = form["product_name"], form["description"] or None
        product.price, product.compare_price = price, compare
        product.category = form["category"] or None
        product.is_available, product.is_featured = form["is_available"], form["is_featured"]
        if image_path:
            old_image, product.image = product.image, image_path
        elif request.form.get("remove_image") and product.image:
            old_image, product.image = product.image, None
        remember_category(db, business, form["category"])
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            delete_upload(image_path)
            flash("Could not update the product. Please try again.", "danger")
            return render_template("seller/product_form.html", business=business, stats=stats, form=form,
                                   suggestions=suggestions, product=product)
        delete_upload(old_image)
        flash("Product updated.", "success")
        return redirect(url_for("seller.products"))
    return render_template("seller/product_form.html", business=business, stats=stats, form=form,
                           suggestions=suggestions, product=product)


@bp.route("/products/<int:product_id>/toggle", methods=["POST"])
@business_required
def toggle_product(product_id):
    db = SessionLocal()
    product = get_own_product_or_404(db, g.business, product_id)
    product.is_available = not product.is_available
    db.commit()
    flash(f"{product.product_name} marked as {'in stock' if product.is_available else 'sold out'}.", "success")
    return redirect(safe_referrer(url_for("seller.products")))


@bp.route("/products/<int:product_id>/duplicate", methods=["POST"])
@business_required
def duplicate_product(product_id):
    db, business = SessionLocal(), g.business
    db.query(Business).filter(Business.id == business.id).with_for_update().one()
    if not business_stats(db, business)["can_add"]:
        db.rollback()
        flash("You have reached your free product limit.", "warning")
        return redirect(url_for("seller.subscription"))
    src = get_own_product_or_404(db, business, product_id)
    new_image = None
    if src.image:
        old_full = os.path.join(S.UPLOAD_ROOT, src.image)
        if os.path.isfile(old_full):
            new_image = f"{src.image.split('/')[0]}/{uuid.uuid4().hex}.webp"
            shutil.copyfile(old_full, os.path.join(S.UPLOAD_ROOT, new_image))
    db.add(Product(business_id=business.id, product_name=f"{src.product_name} (copy)"[:150],
                   description=src.description, price=src.price, compare_price=src.compare_price,
                   image=new_image, category=src.category, is_available=src.is_available))
    db.commit()
    flash("Product duplicated. Edit the copy to change its details.", "success")
    return redirect(url_for("seller.products"))


@bp.route("/products/delete/<int:product_id>", methods=["POST"])
@business_required
def delete_product(product_id):
    db = SessionLocal()
    product = get_own_product_or_404(db, g.business, product_id)
    image = product.image
    db.delete(product)
    db.commit()
    delete_upload(image)
    flash("Product deleted.", "info")
    return redirect(url_for("seller.products"))


# ============================ orders ========================================
def parse_order_form(db, business):
    f = request.form
    form = {k: f.get(k, "").strip() for k in
            ("product_id", "product_name", "quantity", "unit_price", "customer_name", "customer_phone", "notes", "status")}
    errors = []
    product = None
    if form["product_id"]:
        try:
            product = get_own_product_or_404(db, business, int(form["product_id"]))
        except ValueError:
            abort(400)
    name = form["product_name"] or (product.product_name if product else "")
    if not name:
        errors.append("Choose a product or type what was ordered.")
    if len(name) > 150:
        errors.append("Product name is too long.")
    try:
        qty = int(form["quantity"] or 1)
        if not 1 <= qty <= 9999:
            raise ValueError
    except ValueError:
        errors.append("Quantity must be a whole number between 1 and 9999.")
        qty = 1
    price, ok = parse_money(form["unit_price"], allow_empty=True)
    if not ok:
        errors.append("Enter a valid price per item.")
    if price is None:
        price = product.price if product else Decimal(0)
    phone = None
    if form["customer_phone"]:
        phone = normalize_phone(form["customer_phone"])
        if not phone:
            errors.append("Customer phone number is not valid.")
    if len(form["customer_name"]) > 120:
        errors.append("Customer name is too long.")
    if len(form["notes"]) > 500:
        errors.append("Notes are too long (max 500 characters).")
    status = form["status"] if form["status"] in S.ORDER_STATUSES else "new"
    data = dict(product_id=product.id if product else None, product_name=name[:150], quantity=qty,
                unit_price=price, total_amount=price * qty, customer_name=form["customer_name"] or None,
                customer_phone=phone, notes=form["notes"] or None, status=status)
    return form, data, errors


@bp.route("/orders")
@business_required
def orders():
    db, business = SessionLocal(), g.business
    status = request.args.get("status", "")
    q = (request.args.get("q") or "").strip()
    query = db.query(Order).filter(Order.business_id == business.id)
    if status in S.ORDER_STATUSES:
        query = query.filter(Order.status == status)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Order.customer_name.like(like), Order.product_name.like(like),
                                 Order.customer_phone.like(like)))
    page = paginate(query.order_by(Order.id.desc()), 15)
    return render_template("seller/orders.html", business=business, page=page, status=status, q=q,
                           summary=order_summary(db, business.id), stats=business_stats(db, business))


@bp.route("/orders/new", methods=["GET", "POST"])
@business_required
def new_order():
    return _order_form(None)


@bp.route("/orders/<int:order_id>/edit", methods=["GET", "POST"])
@business_required
def edit_order(order_id):
    order = SessionLocal().query(Order).filter(Order.id == order_id, Order.business_id == g.business.id).first()
    if order is None:
        abort(404)
    return _order_form(order)


def _order_form(order):
    db, business = SessionLocal(), g.business
    items = db.query(Product).filter(Product.business_id == business.id).order_by(Product.product_name).all()
    if request.method == "POST":
        form, data, errors = parse_order_form(db, business)
        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("seller/order_form.html", business=business, order=order, form=form, items=items)
        if order is None:
            order = Order(business_id=business.id)
            db.add(order)
        for k, v in data.items():
            setattr(order, k, v)
        db.commit()
        flash("Order saved.", "success")
        return redirect(url_for("seller.orders"))
    if order:
        form = {"product_id": order.product_id or "", "product_name": order.product_name,
                "quantity": order.quantity, "unit_price": "{:f}".format(order.unit_price.normalize()),
                "customer_name": order.customer_name or "", "customer_phone": order.customer_phone or "",
                "notes": order.notes or "", "status": order.status}
    else:
        form = {"product_id": request.args.get("product_id", ""), "quantity": 1, "status": "new",
                "customer_phone": request.args.get("phone", "")}
    return render_template("seller/order_form.html", business=business, order=order, form=form, items=items)


@bp.route("/orders/<int:order_id>/status", methods=["POST"])
@business_required
def order_status(order_id):
    db = SessionLocal()
    order = db.query(Order).filter(Order.id == order_id, Order.business_id == g.business.id).first()
    if order is None:
        abort(404)
    status = request.form.get("status", "")
    if status not in S.ORDER_STATUSES:
        abort(400)
    order.status = status
    db.commit()
    flash(f"Order #{order.id} marked as {status}.", "success")
    return redirect(safe_referrer(url_for("seller.orders")))


@bp.route("/orders/<int:order_id>/delete", methods=["POST"])
@business_required
def delete_order(order_id):
    db = SessionLocal()
    order = db.query(Order).filter(Order.id == order_id, Order.business_id == g.business.id).first()
    if order is None:
        abort(404)
    db.delete(order)
    db.commit()
    flash("Order deleted.", "info")
    return redirect(url_for("seller.orders"))


@bp.route("/orders/export.csv")
@business_required
def export_orders():
    db = SessionLocal()
    if not business_stats(db, g.business)["is_premium"]:
        flash("Exporting orders is a Premium feature.", "warning")
        return redirect(url_for("seller.subscription"))
    rows = db.query(Order).filter(Order.business_id == g.business.id).order_by(Order.id.desc()).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Order #", "Date", "Product", "Qty", "Unit price", "Total", "Customer", "Phone", "Status", "Notes"])

    def safe(v):       # neutralise spreadsheet formula injection
        v = "" if v is None else str(v)
        return "'" + v if v[:1] in "=+-@\t\r" else v

    for o in rows:
        w.writerow([o.id, o.created_at.strftime("%Y-%m-%d %H:%M"), safe(o.product_name), o.quantity,
                    o.unit_price, o.total_amount, safe(o.customer_name), safe(o.customer_phone), o.status, safe(o.notes)])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{g.business.slug}-orders.csv"'})


# ============================ subscription ==================================
@bp.route("/subscription")
@business_required
def subscription():
    db, business = SessionLocal(), g.business
    payments = db.query(Payment).filter(Payment.business_id == business.id).order_by(Payment.id.desc()).limit(10).all()
    mpesa = {"paybill": os.environ.get("MPESA_PAYBILL", "YOUR_PAYBILL"), "account": f"CAT{business.id}",
             "name": os.environ.get("MPESA_ACCOUNT_NAME", S.PLATFORM_NAME)}
    return render_template("seller/subscription.html", business=business, stats=business_stats(db, business),
                           payments=payments, pending=any(p.status == "pending" for p in payments), mpesa=mpesa)


@bp.route("/payment/submit", methods=["POST"])
@business_required
def submit_payment():
    """Records a PENDING payment. Never grants premium - only an admin can."""
    db, business = SessionLocal(), g.business
    code = re.sub(r"\s+", "", request.form.get("transaction_reference", "")).upper()
    back = redirect(url_for("seller.subscription"))
    if S.MAIL_ENABLED and not g.user.email_verified:
        flash("Please confirm your email address before submitting a payment.", "warning")
        return back
    if not MPESA_CODE_RE.match(code):
        flash("Enter the 10-character M-Pesa transaction code from your confirmation SMS (e.g. SHK7XY12AB).", "danger")
        return back
    if db.query(Payment.id).filter(Payment.business_id == business.id, Payment.status == "pending").first():
        flash("You already have a payment waiting for confirmation. Please wait for it to be verified.", "info")
        return back
    if db.query(Payment.id).filter(Payment.transaction_reference == code).first():
        flash("That transaction code has already been submitted.", "danger")
        return back
    db.add(Payment(business_id=business.id, amount=Decimal(S.PREMIUM_PRICE), payment_method="mpesa",
                   transaction_reference=code, status="pending"))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        flash("That transaction code has already been submitted.", "danger")
        return back
    flash("Payment submitted. Premium will be activated once we verify your M-Pesa code.", "success")
    return back


# ============================ account =======================================
@bp.route("/account", methods=["GET", "POST"])
@seller_required
def account():
    db, user = SessionLocal(), g.user
    form = {"full_name": user.full_name, "email": user.email or "", "phone": user.phone or ""}
    if request.method == "POST":
        action = request.form.get("action")
        password_ok = check_password_hash(user.password_hash, request.form.get("current_password", ""))

        if action == "profile":
            form = {"full_name": request.form.get("full_name", "").strip(),
                    "email": request.form.get("email", "").strip().lower(),
                    "phone": request.form.get("phone", "").strip()}
            errors = []
            phone = normalize_phone(form["phone"])
            if not 2 <= len(form["full_name"]) <= 120:
                errors.append("Please enter your full name.")
            if not EMAIL_RE.match(form["email"]) or len(form["email"]) > 190:
                errors.append("Please enter a valid email address.")
            if not phone:
                errors.append("Please enter a valid phone number.")
            changed_login = form["email"] != user.email or phone != user.phone
            if changed_login and not password_ok:
                errors.append("Enter your current password to change your email or phone number.")
            if not errors:
                if form["email"] != user.email and db.query(User.id).filter(User.email == form["email"]).first():
                    errors.append("That email is already used by another account.")
                if phone != user.phone and db.query(User.id).filter(User.phone == phone).first():
                    errors.append("That phone number is already used by another account.")
            if errors:
                for e in errors:
                    flash(e, "danger")
            else:
                email_changed = form["email"] != user.email
                user.full_name, user.email, user.phone = form["full_name"], form["email"], phone
                if email_changed:
                    user.email_verified = False
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()
                    flash("That email or phone is already in use.", "danger")
                    return render_template("seller/account.html", form=form)
                if email_changed:
                    send_verification_email(user)
                    flash("Saved. We sent a confirmation link to your new email address.", "success")
                else:
                    flash("Account details saved.", "success")
                return redirect(url_for("seller.account"))

        elif action == "password":
            new, confirm = request.form.get("new_password", ""), request.form.get("confirm_password", "")
            if not password_ok:
                flash("Your current password is incorrect.", "danger")
            elif len(new) < 8 or len(new) > 128:
                flash("New password must be between 8 and 128 characters.", "danger")
            elif new != confirm:
                flash("New passwords do not match.", "danger")
            else:
                user.password_hash = generate_password_hash(new)
                db.commit()
                flash("Password changed.", "success")
                return redirect(url_for("seller.account"))

        elif action == "delete":
            if not password_ok or request.form.get("confirm_text", "").strip().upper() != "DELETE":
                flash("To delete your account enter your password and type DELETE.", "danger")
            else:
                biz = db.query(Business).filter(Business.user_id == user.id).first()
                files = []
                if biz:
                    files = [biz.logo] + [p.image for p in biz.products]
                db.delete(user)
                db.commit()
                for f in files:
                    delete_upload(f)
                session.clear()
                flash("Your account and all its data were deleted.", "info")
                return redirect(url_for("public.index"))
    return render_template("seller/account.html", form=form)
