
"""Registration, login, logout, password reset, email verification."""

from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

import hashlib
import secrets
from datetime import timedelta

from connection import SessionLocal
from core import (
    EMAIL_RE,
    client_ip,
    log_admin,
    normalize_phone,
    rate_clear,
    rate_count,
    rate_hit,
    read_reset_token,
    safe_referrer,
    read_verify_token,
    seller_required,
    send_reset_email,
    send_verification_email,
    send_login_otp_email,
    utcnow,
)
from models import User


bp = Blueprint("auth", __name__)

_DUMMY_HASH = generate_password_hash("not-a-real-password")
WINDOW = 600


def start_session(user):
    session.clear()
    session["user_id"] = user.id
    session.permanent = True


def _home(user):
    return url_for(
        "admin.dashboard" if user.role == "admin" else "seller.dashboard"
    )


def _validate_password(password, confirm):
    errors = []

    if len(password) < 8:
        errors.append("Password must be at least 8 characters.")

    if len(password) > 128:
        errors.append("Password is too long.")

    if password != confirm:
        errors.append("Passwords do not match.")

    return errors


# -------------------------------------------------------------------
# REGISTRATION
# -------------------------------------------------------------------

@bp.route("/register", methods=["GET", "POST"])
def register():

    if g.user:
        return redirect(_home(g.user))

    form = {}

    if request.method == "POST":

        db = SessionLocal()

        form = {
            "full_name": request.form.get("full_name", "").strip(),
            "email": request.form.get("email", "").strip().lower(),
            "phone": request.form.get("phone", "").strip(),
        }

        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        errors = []

        if rate_count(db, f"reg:{client_ip()}", 3600) >= 10:
            flash(
                "Too many sign-ups from this connection. Please try again later.",
                "danger"
            )
            return render_template("auth/register.html", form=form), 429

        if not request.form.get("terms"):
            errors.append("Please accept the Terms and Privacy Policy.")

        if len(form["full_name"]) < 2 or len(form["full_name"]) > 120:
            errors.append("Please enter your full name.")

        if not EMAIL_RE.match(form["email"]) or len(form["email"]) > 190:
            errors.append("Please enter a valid email address.")

        phone = normalize_phone(form["phone"])

        if not phone:
            errors.append("Please enter a valid phone number, e.g. 0712 345 678.")

        errors += _validate_password(password, confirm)

        if not errors:

            if db.query(User.id).filter(User.email == form["email"]).first():
                errors.append("An account with this email already exists.")

            if db.query(User.id).filter(User.phone == phone).first():
                errors.append("An account with this phone number already exists.")

        if not errors:

            user = User(
                full_name=form["full_name"],
                email=form["email"],
                phone=phone,
                password_hash=generate_password_hash(password),
                role="seller",
                is_active=True,
                email_verified=False,
            )

            db.add(user)

            try:
                db.commit()

            except IntegrityError:
                db.rollback()
                errors.append(
                    "An account with this email or phone already exists."
                )

            else:
                rate_hit(db, f"reg:{client_ip()}")
                send_verification_email(user)

                flash(
                    "Your account has been created. Please verify your email before logging in.",
                    "success"
                )

                return redirect(url_for("auth.login"))

        for error in errors:
            flash(error, "danger")

    return render_template("auth/register.html", form=form)


# -------------------------------------------------------------------
# SHARED LOGIN FUNCTION
# -------------------------------------------------------------------

def _do_login(template, role, identifier, password, form, bucket_prefix=""):
    """Shared by seller and admin login."""

    db = SessionLocal()

    ip_bucket = f"loginip:{client_ip()}"
    key_bucket = f"login:{bucket_prefix}{client_ip()}|{identifier.lower()}"

    if (
        rate_count(db, key_bucket, WINDOW) >= 5
        or rate_count(db, ip_bucket, WINDOW) >= 40
    ):
        flash(
            "Too many failed attempts. Please wait 10 minutes and try again.",
            "danger"
        )
        return render_template(template, form=form), 429, None

    user = None

    if "@" in identifier:
        user = db.query(User).filter(
            User.email == identifier.lower()
        ).first()

    elif role == "seller":
        phone = normalize_phone(identifier)

        if phone:
            user = db.query(User).filter(
                User.phone == phone
            ).first()

    valid = check_password_hash(
        user.password_hash if user else _DUMMY_HASH,
        password
    )

    if user and valid and user.role == role:

        if not user.is_active:
            flash(
                "This account has been suspended. Please contact support.",
                "danger"
            )
            return render_template(template, form=form), 200, None

        if not user.email:
            flash(
                "This account needs an email address before email verification can be used. Contact support.",
                "danger"
            )
            return render_template(template, form=form), 200, None

        if role == "seller" and not user.email_verified:
            send_verification_email(user)

            flash(
                "Please verify your email first. We have sent you a confirmation link.",
                "warning"
            )

            return render_template(template, form=form), 200, None

        rate_clear(db, key_bucket)

        code = f"{secrets.randbelow(1000000):06d}"

        session.clear()

        session["pending_login"] = {
            "uid": user.id,
            "code_hash": hashlib.sha256(code.encode()).hexdigest(),
            "expires": (
                utcnow() + timedelta(minutes=5)
            ).timestamp(),
            "attempts": 0,
            "sent_at": utcnow().timestamp(),
            "role": role,
        }

        send_login_otp_email(user, code)

        return None, 200, "otp_pending"

    rate_hit(db, key_bucket)
    rate_hit(db, ip_bucket)

    flash(
        "Invalid admin credentials."
        if role == "admin"
        else "Incorrect email/phone or password.",
        "danger"
    )

    return render_template(template, form=form), 200, None


# -------------------------------------------------------------------
# LOGIN OTP VERIFICATION
# -------------------------------------------------------------------

@bp.route("/verify-login-otp", methods=["GET", "POST"])
def verify_login_otp():

    pending = session.get("pending_login")

    if not pending:
        flash(
            "Please sign in again to request a verification code.",
            "warning"
        )
        return redirect(url_for("auth.login"))

    if request.method == "POST":

        if utcnow().timestamp() > pending.get("expires", 0):
            session.pop("pending_login", None)

            flash(
                "Your code has expired. Please sign in again.",
                "danger"
            )

            return redirect(url_for("auth.login"))

        pending["attempts"] = pending.get("attempts", 0) + 1

        code = request.form.get("code", "").strip()

        if pending["attempts"] > 5:
            session.pop("pending_login", None)

            flash(
                "Too many incorrect codes. Please sign in again.",
                "danger"
            )

            return redirect(url_for("auth.login"))

        valid_code = (
            len(code) == 6
            and code.isdigit()
            and secrets.compare_digest(
                hashlib.sha256(code.encode()).hexdigest(),
                pending.get("code_hash", "")
            )
        )

        if not valid_code:

            session["pending_login"] = pending

            if pending["attempts"] >= 5:
                session.pop("pending_login", None)

                flash(
                    "Too many incorrect codes. Please sign in again.",
                    "danger"
                )

                return redirect(url_for("auth.login"))

            flash(
                "Incorrect verification code. Please try again.",
                "danger"
            )

            return render_template("auth/otp.html"), 200

        db = SessionLocal()

        user = db.get(User, pending.get("uid"))

        if (
            not user
            or not user.is_active
            or user.role != pending.get("role")
        ):
            session.clear()

            flash(
                "This account cannot sign in. Please contact support.",
                "danger"
            )

            return redirect(url_for("auth.login"))

        user.last_login_at = utcnow()

        if user.role == "admin":
            log_admin(
                db,
                user.id,
                "admin_login",
                "Admin logged in after email OTP verification."
            )

        db.commit()

        role = user.role

        session.clear()
        start_session(user)

        flash("Login verified successfully.", "success")

        return redirect(
            url_for(
                "admin.dashboard"
                if role == "admin"
                else "seller.dashboard"
            )
        )

    return render_template("auth/otp.html")


# -------------------------------------------------------------------
# RESEND LOGIN OTP
# -------------------------------------------------------------------

@bp.route("/resend-login-otp", methods=["POST"])
def resend_login_otp():

    pending = session.get("pending_login")

    if not pending:
        return redirect(url_for("auth.login"))

    now = utcnow().timestamp()

    if now - pending.get("sent_at", 0) < 60:
        flash(
            "Please wait 60 seconds before requesting another code.",
            "warning"
        )
        return redirect(url_for("auth.verify_login_otp"))

    db = SessionLocal()

    user = db.get(User, pending.get("uid"))

    if (
        not user
        or not user.is_active
        or user.role != pending.get("role")
    ):
        session.clear()
        return redirect(url_for("auth.login"))

    code = f"{secrets.randbelow(1000000):06d}"

    pending.update(
        code_hash=hashlib.sha256(code.encode()).hexdigest(),
        expires=(utcnow() + timedelta(minutes=5)).timestamp(),
        attempts=0,
        sent_at=now,
    )

    session["pending_login"] = pending

    send_login_otp_email(user, code)

    flash(
        "A new verification code has been sent to your email.",
        "success"
    )

    return redirect(url_for("auth.verify_login_otp"))


# -------------------------------------------------------------------
# SELLER LOGIN
# -------------------------------------------------------------------

@bp.route("/login", methods=["GET", "POST"])
def login():

    if g.user:
        return redirect(_home(g.user))

    form = {
        "next": request.args.get("next", "")
    }

    if request.method == "POST":

        identifier = request.form.get("identifier", "").strip()

        form = {
            "identifier": identifier,
            "next": request.form.get("next", ""),
        }

        body, code, result = _do_login(
            "auth/login.html",
            "seller",
            identifier,
            request.form.get("password", ""),
            form,
        )

        if result == "otp_pending":
            flash(
                "We sent a sign-in code to your email.",
                "success"
            )
            return redirect(url_for("auth.verify_login_otp"))

        return body, code

    return render_template("auth/login.html", form=form)


# -------------------------------------------------------------------
# ADMIN LOGIN
# -------------------------------------------------------------------

@bp.route("/admin/login", methods=["GET", "POST"])
def admin_login():

    if g.user and g.user.role == "admin":
        return redirect(url_for("admin.dashboard"))

    form = {}

    if request.method == "POST":

        email = request.form.get("email", "").strip().lower()

        form = {
            "email": email
        }

        body, code, result = _do_login(
            "auth/admin_login.html",
            "admin",
            email,
            request.form.get("password", ""),
            form,
            bucket_prefix="admin:",
        )

        if result == "otp_pending":
            flash(
                "We sent a sign-in code to your email.",
                "success"
            )
            return redirect(url_for("auth.verify_login_otp"))

        return body, code

    return render_template("auth/admin_login.html", form=form)


# -------------------------------------------------------------------
# LOGOUT
# -------------------------------------------------------------------

@bp.route("/logout", methods=["POST"])
def logout():

    session.clear()

    flash("You have been logged out.", "info")

    return redirect(url_for("public.index"))


# -------------------------------------------------------------------
# FORGOT PASSWORD
# -------------------------------------------------------------------

@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():

    if request.method == "POST":

        db = SessionLocal()

        email = request.form.get("email", "").strip().lower()

        ip_bucket = f"reset:{client_ip()}"
        mail_bucket = f"reset-mail:{email}"

        if (
            rate_count(db, ip_bucket, 3600) >= 10
            or rate_count(db, mail_bucket, 3600) >= 3
        ):
            flash(
                "Too many reset requests. Please try again in an hour.",
                "danger"
            )

            return render_template("auth/forgot.html"), 429

        rate_hit(db, ip_bucket)
        rate_hit(db, mail_bucket)

        user = (
            db.query(User).filter(User.email == email).first()
            if EMAIL_RE.match(email)
            else None
        )

        if user and user.is_active:
            send_reset_email(user)

        flash(
            "If an account exists for that email, a reset link is on its way. It is valid for 1 hour.",
            "info"
        )

        return redirect(url_for("auth.login"))

    return render_template("auth/forgot.html")


# -------------------------------------------------------------------
# RESET PASSWORD
# -------------------------------------------------------------------

@bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):

    db = SessionLocal()

    user = read_reset_token(db, token)

    if user is None:
        flash(
            "That reset link is invalid or has expired. Please request a new one.",
            "danger"
        )
        return redirect(url_for("auth.forgot_password"))

    if request.method == "POST":

        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        errors = _validate_password(password, confirm)

        if errors:
            for error in errors:
                flash(error, "danger")

            return render_template("auth/reset.html", token=token)

        user.password_hash = generate_password_hash(password)
        user.email_verified = True

        db.commit()

        rate_clear(
            db,
            f"login:{request.remote_addr}|{(user.email or '').lower()}"
        )

        flash(
            "Password updated. You can now log in.",
            "success"
        )

        return redirect(
            url_for(
                "auth.admin_login"
                if user.role == "admin"
                else "auth.login"
            )
        )

    return render_template("auth/reset.html", token=token)


# -------------------------------------------------------------------
# EMAIL VERIFICATION
# -------------------------------------------------------------------

@bp.route("/verify-email/<token>")
def verify_email(token):

    db = SessionLocal()

    user = read_verify_token(db, token)

    if user is None:
        flash(
            "That confirmation link is invalid or has expired. Log in and request a new one.",
            "danger"
        )
        return redirect(url_for("auth.login"))

    if not user.email_verified:
        user.email_verified = True
        db.commit()

    flash(
        "Email confirmed. Thank you! You can now log in.",
        "success"
    )

    # Only redirect to the seller dashboard if the logged-in user
    # is the same seller whose email was verified.
    if (
        g.user
        and g.user.id == user.id
        and g.user.role == "seller"
    ):
        return redirect(url_for("seller.dashboard"))

    return redirect(url_for("auth.login"))


# -------------------------------------------------------------------
# RESEND EMAIL VERIFICATION
# -------------------------------------------------------------------

@bp.route("/resend-verification", methods=["POST"])
@seller_required
def resend_verification():

    db = SessionLocal()

    if g.user.email_verified:
        return redirect(url_for("seller.dashboard"))

    bucket = f"verify:{g.user.id}"

    if rate_count(db, bucket, 3600) >= 3:

        flash(
            "You have requested too many emails. Please try again later.",
            "warning"
        )

    else:

        rate_hit(db, bucket)

        send_verification_email(g.user)

        flash(
            f"Confirmation email sent to {g.user.email}.",
            "success"
        )

    return redirect(
        safe_referrer(url_for("seller.dashboard"))
    )
