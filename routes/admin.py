"""Admin area: dashboard, users, payments, activity log."""
import csv
import io
from collections import defaultdict
from datetime import date, timedelta
from urllib.parse import quote

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import func, or_

import settings as S
from connection import SessionLocal
from core import (active_premium, admin_required, analytics_summary, business_stats, fmt_dt, grant_premium,
                  log_admin, make_reset_token, order_summary, page_url, paginate, revoke_premium,
                  safe_referrer, to_eat, utcnow)
from models import AdminLog, Business, Event, Order, Payment, Product, Subscription, User

bp = Blueprint("admin", __name__, url_prefix="/admin")
bp.add_app_template_global(page_url, "admin_page_url")


def _premium_ids(db):
    return (db.query(Subscription.business_id)
            .filter(Subscription.plan_type == "premium", Subscription.status == "active",
                    Subscription.expiry_date > utcnow()))


# ============================ dashboard =====================================
@bp.route("/dashboard")
@admin_required
def dashboard():
    db, now = SessionLocal(), utcnow()
    premium_q = _premium_ids(db)
    premium_businesses = db.query(func.count(func.distinct(Subscription.business_id))).filter(
        Subscription.plan_type == "premium", Subscription.status == "active",
        Subscription.expiry_date > now).scalar() or 0
    total_sellers = db.query(func.count(User.id)).filter(User.role == "seller").scalar() or 0
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    revenue_total = db.query(func.coalesce(func.sum(Payment.amount), 0)).filter(Payment.status == "approved").scalar() or 0
    revenue_month = db.query(func.coalesce(func.sum(Payment.amount), 0)).filter(
        Payment.status == "approved", Payment.payment_date >= month_start).scalar() or 0
    week_ago = now - timedelta(days=7)
    stats = {
        "sellers": total_sellers,
        "new_week": db.query(func.count(User.id)).filter(User.role == "seller", User.created_at >= week_ago).scalar() or 0,
        "businesses": db.query(func.count(Business.id)).scalar() or 0,
        "products": db.query(func.count(Product.id)).scalar() or 0,
        "orders": db.query(func.count(Order.id)).scalar() or 0,
        "premium": premium_businesses,
        "free": max(0, total_sellers - premium_businesses),
        "conversion": round(premium_businesses * 100 / total_sellers) if total_sellers else 0,
        "pending": db.query(func.count(Payment.id)).filter(Payment.status == "pending").scalar() or 0,
        "revenue_total": revenue_total, "revenue_month": revenue_month,
        "views_30d": db.query(func.count(Event.id)).filter(Event.event_type == "view",
                                                           Event.created_at >= now - timedelta(days=30)).scalar() or 0,
        "unverified": db.query(func.count(User.id)).filter(User.role == "seller", User.email_verified.is_(False)).scalar() or 0,
    }

    # signups per day (last 14 days, East Africa time)
    start = (to_eat(now) - timedelta(days=13)).date()
    per_day = defaultdict(int)
    for (created,) in db.query(User.created_at).filter(User.role == "seller",
                                                       User.created_at >= now - timedelta(days=15)):
        per_day[to_eat(created).date()] += 1
    signups = [{"label": (start + timedelta(days=i)).strftime("%d %b"), "n": per_day[start + timedelta(days=i)]}
               for i in range(14)]
    peak = max([s["n"] for s in signups] + [1])
    for s in signups:
        s["h"] = int(s["n"] * 100 / peak)

    # revenue per month (last 6)
    months, cursor = [], to_eat(now).replace(day=1)
    for _ in range(6):
        months.append((cursor.year, cursor.month))
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    months.reverse()
    totals = defaultdict(int)
    for amount, paid in db.query(Payment.amount, Payment.payment_date).filter(Payment.status == "approved",
                                                                              Payment.payment_date.isnot(None)):
        d = to_eat(paid)
        totals[(d.year, d.month)] += int(amount)
    revenue = [{"label": date(y, m, 1).strftime("%b"), "n": totals[(y, m)]} for y, m in months]
    peak = max([r["n"] for r in revenue] + [1])
    for r in revenue:
        r["h"] = int(r["n"] * 100 / peak)

    pending = (db.query(Payment, Business, User).join(Business, Payment.business_id == Business.id)
               .join(User, Business.user_id == User.id).filter(Payment.status == "pending")
               .order_by(Payment.id.asc()).limit(8).all())
    expiring = (db.query(Subscription, Business, User).join(Business, Subscription.business_id == Business.id)
                .join(User, Business.user_id == User.id)
                .filter(Subscription.plan_type == "premium", Subscription.status == "active",
                        Subscription.expiry_date > now, Subscription.expiry_date <= now + timedelta(days=7))
                .order_by(Subscription.expiry_date.asc()).limit(6).all())
    recent_users = db.query(User).filter(User.role == "seller").order_by(User.id.desc()).limit(6).all()
    top = (db.query(Business, func.count(Event.id).label("n")).join(Event, Event.business_id == Business.id)
           .filter(Event.event_type == "view", Event.created_at >= now - timedelta(days=30))
           .group_by(Business.id).order_by(func.count(Event.id).desc()).limit(5).all())
    logs = db.query(AdminLog).order_by(AdminLog.id.desc()).limit(6).all()
    return render_template("admin/dashboard.html", stats=stats, signups=signups, revenue=revenue, pending=pending,
                           expiring=expiring, recent_users=recent_users, top=top, logs=logs)


# ============================ users =========================================
@bp.route("/users")
@admin_required
def users():
    db, now = SessionLocal(), utcnow()
    q = (request.args.get("q") or "").strip()
    flt = request.args.get("filter", "")
    prem = _premium_ids(db)
    query = (db.query(User).outerjoin(Business, Business.user_id == User.id).filter(User.role == "seller"))
    if q:
        like = f"%{q}%"
        query = query.filter(or_(User.full_name.like(like), User.email.like(like), User.phone.like(like),
                                 Business.business_name.like(like)))
    if flt == "premium":
        query = query.filter(Business.id.in_(prem))
    elif flt == "free":
        query = query.filter(or_(Business.id.is_(None), Business.id.notin_(prem)))
    elif flt == "suspended":
        query = query.filter(User.is_active.is_(False))
    elif flt == "unverified":
        query = query.filter(User.email_verified.is_(False))
    elif flt == "nobusiness":
        query = query.filter(Business.id.is_(None))
    page = paginate(query.order_by(User.id.desc()), 20)

    ids = [u.id for u in page.items]
    businesses = {b.user_id: b for b in db.query(Business).filter(Business.user_id.in_(ids or [0]))}
    bids = [b.id for b in businesses.values()] or [0]
    counts = dict(db.query(Product.business_id, func.count(Product.id)).filter(Product.business_id.in_(bids))
                  .group_by(Product.business_id).all())
    expiries = dict(db.query(Subscription.business_id, func.max(Subscription.expiry_date))
                    .filter(Subscription.business_id.in_(bids), Subscription.plan_type == "premium",
                            Subscription.status == "active").group_by(Subscription.business_id).all())
    rows = []
    for u in page.items:
        b = businesses.get(u.id)
        exp = expiries.get(b.id) if b else None
        rows.append({"user": u, "business": b, "products": counts.get(b.id, 0) if b else 0,
                     "expiry": exp, "is_premium": bool(exp and exp > now)})
    return render_template("admin/users.html", rows=rows, page=page, q=q, flt=flt)


def _user_or_404(db, user_id):
    user = db.get(User, user_id)
    if user is None:
        abort(404)
    return user


@bp.route("/users/<int:user_id>")
@admin_required
def user_detail(user_id, reset_link=None):
    db = SessionLocal()
    user = _user_or_404(db, user_id)
    business = db.query(Business).filter(Business.user_id == user.id).first()
    ctx = {"u": user, "business": business, "reset_link": reset_link, "reset_wa": None}
    if reset_link and user.phone:
        ctx["reset_wa"] = f"https://wa.me/{user.phone}?text=" + quote(
            f"Hello {user.full_name}, use this link to set a new password for {S.PLATFORM_NAME} "
            f"(valid for 1 hour): {reset_link}", safe="")
    if business:
        ctx.update(
            stats=business_stats(db, business),
            payments=db.query(Payment).filter(Payment.business_id == business.id).order_by(Payment.id.desc()).limit(10).all(),
            subs=db.query(Subscription).filter(Subscription.business_id == business.id, Subscription.plan_type == "premium")
            .order_by(Subscription.id.desc()).limit(10).all(),
            traffic=analytics_summary(db, business.id, 30),
            orders=order_summary(db, business.id))
    return render_template("admin/user_detail.html", **ctx)


@bp.route("/users/<int:user_id>/toggle", methods=["POST"])
@admin_required
def toggle_user(user_id):
    db = SessionLocal()
    user = _user_or_404(db, user_id)
    if user.role == "admin":
        flash("Admin accounts cannot be suspended from here.", "danger")
        return redirect(url_for("admin.users"))
    user.is_active = not user.is_active
    log_admin(db, g.user.id, "activate_user" if user.is_active else "suspend_user",
              f"User #{user.id} ({user.email}) set to {'active' if user.is_active else 'suspended'}.")
    db.commit()
    flash(f"{user.full_name} is now {'active' if user.is_active else 'suspended'}.", "success")
    return redirect(safe_referrer(url_for("admin.users")))


@bp.route("/users/<int:user_id>/verify", methods=["POST"])
@admin_required
def verify_user(user_id):
    db = SessionLocal()
    user = _user_or_404(db, user_id)
    user.email_verified = True
    log_admin(db, g.user.id, "verify_email", f"Marked email of user #{user.id} as verified.")
    db.commit()
    flash("Email marked as verified.", "success")
    return redirect(safe_referrer(url_for("admin.users")))


@bp.route("/users/<int:user_id>/reset-link", methods=["POST"])
@admin_required
def reset_link(user_id):
    """Support tool for sellers who cannot receive email: generates a one-hour reset link."""
    db = SessionLocal()
    user = _user_or_404(db, user_id)
    if user.role == "admin":
        flash("Use the normal forgot-password page for admin accounts.", "danger")
        return redirect(url_for("admin.user_detail", user_id=user.id))
    link = url_for("auth.reset_password", token=make_reset_token(user), _external=True)
    log_admin(db, g.user.id, "reset_link", f"Generated password reset link for user #{user.id}.")
    db.commit()
    return user_detail(user_id, reset_link=link)


@bp.route("/users/<int:user_id>/grant", methods=["POST"])
@admin_required
def grant(user_id):
    db = SessionLocal()
    user = _user_or_404(db, user_id)
    business = db.query(Business).filter(Business.user_id == user.id).first()
    if business is None:
        flash("This user has no business yet.", "danger")
        return redirect(url_for("admin.user_detail", user_id=user.id))
    try:
        days = int(request.form.get("days", ""))
        if not 1 <= days <= 365:
            raise ValueError
    except ValueError:
        flash("Enter a number of days between 1 and 365.", "danger")
        return redirect(url_for("admin.user_detail", user_id=user.id))
    reason = (request.form.get("reason") or "").strip()[:150] or "no reason given"
    grant_premium(db, business, days, g.user.id, "admin", "grant_premium",
                  f"Granted {days} days to business #{business.id} ({reason}).")
    db.commit()
    flash(f"Granted {days} days of Premium to {business.business_name}.", "success")
    return redirect(url_for("admin.user_detail", user_id=user.id))


@bp.route("/users/<int:user_id>/revoke", methods=["POST"])
@admin_required
def revoke(user_id):
    db = SessionLocal()
    user = _user_or_404(db, user_id)
    business = db.query(Business).filter(Business.user_id == user.id).first()
    if business and revoke_premium(db, business, g.user.id):
        db.commit()
        flash("Premium revoked.", "info")
    else:
        flash("This business has no active Premium.", "info")
    return redirect(url_for("admin.user_detail", user_id=user.id))


# ============================ payments ======================================
def _payments_query(db):
    status = request.args.get("status", "")
    q = (request.args.get("q") or "").strip().upper()
    query = (db.query(Payment, Business, User).join(Business, Payment.business_id == Business.id)
             .join(User, Business.user_id == User.id))
    if status in ("pending", "approved", "rejected"):
        query = query.filter(Payment.status == status)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Payment.transaction_reference.like(like), func.upper(Business.business_name).like(like),
                                 func.upper(User.full_name).like(like), func.upper(User.phone).like(like)))
    return query, status, q


@bp.route("/payments")
@admin_required
def payments():
    db = SessionLocal()
    query, status, q = _payments_query(db)
    page = paginate(query.order_by(Payment.id.desc()), 20)
    return render_template("admin/payments.html", page=page, status=status, q=q)


@bp.route("/payments/export.csv")
@admin_required
def export_payments():
    db = SessionLocal()
    query, _, _ = _payments_query(db)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ID", "Submitted (EAT)", "Business", "Account", "Seller", "Phone", "M-Pesa code", "Amount", "Status", "Note"])
    for p, b, u in query.order_by(Payment.id.desc()).limit(5000).all():
        w.writerow([p.id, fmt_dt(p.created_at), b.business_name, f"CAT{b.id}", u.full_name, u.phone,
                    p.transaction_reference, p.amount, p.status, p.note or ""])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="payments.csv"'})


def _locked_pending_payment(db, payment_id):
    payment = db.query(Payment).filter(Payment.id == payment_id).with_for_update().first()
    if payment is None:
        abort(404)
    if payment.status != "pending":
        db.rollback()
        flash("This payment has already been processed.", "info")
        return None
    return payment


@bp.route("/payment/approve/<int:payment_id>", methods=["POST"])
@admin_required
def approve_payment(payment_id):
    db = SessionLocal()
    payment = _locked_pending_payment(db, payment_id)
    if payment:
        business = db.get(Business, payment.business_id)
        payment.status, payment.payment_date, payment.reviewed_by = "approved", utcnow(), g.user.id
        grant_premium(db, business, S.PREMIUM_DAYS, g.user.id, "payment", "approve_payment",
                      f"Approved payment #{payment.id} ({payment.transaction_reference}) for business #{business.id}.")
        db.commit()
        flash(f"Payment {payment.transaction_reference} approved. Premium activated.", "success")
    return redirect(safe_referrer(url_for("admin.payments")))


@bp.route("/payment/reject/<int:payment_id>", methods=["POST"])
@admin_required
def reject_payment(payment_id):
    db = SessionLocal()
    payment = _locked_pending_payment(db, payment_id)
    if payment:
        payment.status, payment.reviewed_by = "rejected", g.user.id
        payment.note = (request.form.get("note") or "").strip()[:250] or "Payment could not be verified."
        log_admin(db, g.user.id, "reject_payment",
                  f"Rejected payment #{payment.id} ({payment.transaction_reference}): {payment.note}")
        db.commit()
        flash(f"Payment {payment.transaction_reference} rejected.", "info")
    return redirect(safe_referrer(url_for("admin.payments")))


# ============================ activity log ==================================
@bp.route("/activity")
@admin_required
def activity():
    db = SessionLocal()
    page = paginate(db.query(AdminLog).order_by(AdminLog.id.desc()), 30)
    return render_template("admin/activity.html", page=page)
