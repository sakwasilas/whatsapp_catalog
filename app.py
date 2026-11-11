"""WhatsApp Catalog Maker - application factory and request lifecycle.

Routes live in routes/ (public, auth, seller, admin). Shared helpers in core.py.
Run:  python app.py      or      gunicorn -w 3 "app:create_app()"
"""
import os
import secrets
from datetime import timedelta

import click
from flask import Flask, abort, g, render_template, request, session
from sqlalchemy import func
from werkzeug.middleware.proxy_fix import ProxyFix

import settings as S
from connection import SessionLocal
from models import Business, Event, Payment, RateEvent, User


def load_secret_key():
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    if S.IS_PRODUCTION:
        raise RuntimeError("SECRET_KEY environment variable must be set in production.")
    path = os.path.join(S.BASE_DIR, ".secret_key")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    key = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(key)
    return key


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(32)
    return session["_csrf"]


def create_app(test_config=None):
    from core import register_template_helpers, utcnow
    from routes import admin, auth, public, seller

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=load_secret_key(),
        MAX_CONTENT_LENGTH=S.MAX_REQUEST_BYTES,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=S.IS_PRODUCTION or os.environ.get("SESSION_COOKIE_SECURE") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=14),
    )
    if test_config:
        app.config.update(test_config)
    if os.environ.get("TRUST_PROXY", "0") == "1":      # behind nginx / a PaaS router
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    for d in (S.PRODUCT_UPLOAD_DIR, S.PROFILE_UPLOAD_DIR):
        os.makedirs(d, exist_ok=True)

    register_template_helpers(app)
    app.jinja_env.globals["csrf_token"] = csrf_token

    for bp in (public.bp, auth.bp, seller.bp, admin.bp):
        app.register_blueprint(bp)

    @app.before_request
    def before_request():
        g.user = None
        if request.endpoint in (None, "static", "public.uploaded_file"):
            return None
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
            expected = session.get("_csrf", "")
            if not expected or not secrets.compare_digest(sent, expected):
                abort(400, description="Your session expired or the form was invalid. Please go back and try again.")
        user_id = session.get("user_id")
        if user_id:
            user = SessionLocal().get(User, user_id)
            if user and user.is_active:
                g.user = user
            else:
                session.pop("user_id", None)

    @app.after_request
    def security_headers(response):
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "SAMEORIGIN")
        h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        h.setdefault("Content-Security-Policy",
                     "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                     "script-src 'self'; font-src 'self'; frame-ancestors 'self'; base-uri 'self'; form-action 'self'")
        if S.IS_PRODUCTION:
            h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.endpoint not in ("static", "public.uploaded_file") and request.method == "GET" and g.get("user"):
            h["Cache-Control"] = "no-store"
        return response

    @app.teardown_appcontext
    def shutdown_session(exception=None):
        SessionLocal.remove()

    @app.context_processor
    def inject_globals():
        extra = {"nav_business": None, "pending_count": 0}
        user = g.get("user")
        if user is not None:
            db = SessionLocal()
            if user.role == "admin":
                extra["pending_count"] = db.query(func.count(Payment.id)).filter(Payment.status == "pending").scalar() or 0
            else:
                extra["nav_business"] = db.query(Business).filter(Business.user_id == user.id).first()
        return {**extra,
            "current_user": g.get("user"), "FREE_LIMIT": S.FREE_PRODUCT_LIMIT,
            "PREMIUM_PRICE": S.PREMIUM_PRICE, "PREMIUM_DAYS": S.PREMIUM_DAYS,
            "PLATFORM_NAME": S.PLATFORM_NAME, "SUPPORT_WHATSAPP": S.SUPPORT_WHATSAPP,
            "MAIL_ENABLED": S.MAIL_ENABLED, "THEME_COLORS": S.THEME_COLORS,
            "current_year": utcnow().year,
        }

    def _error(code, title, message):
        return render_template("error.html", code=code, title=title, message=message), code

    @app.errorhandler(400)
    def bad_request(e):
        return _error(400, "Bad request", getattr(e, "description", None) or "The request could not be processed.")

    @app.errorhandler(403)
    def forbidden(e):
        return _error(403, "Access denied", "You do not have permission to view this page.")

    @app.errorhandler(404)
    def not_found(e):
        return _error(404, "Page not found", "The page you are looking for does not exist.")

    @app.errorhandler(405)
    def not_allowed(e):
        return _error(405, "Not allowed", "That action is not allowed here.")

    @app.errorhandler(413)
    def too_large(e):
        return _error(413, "File too large", "The upload is too large. Images must be 4 MB or smaller.")

    @app.errorhandler(500)
    def server_error(e):
        SessionLocal.rollback()
        return _error(500, "Something went wrong", "An unexpected error occurred. Please try again in a moment.")

    @app.cli.command("cleanup")
    def cleanup():
        """Delete old analytics events and rate-limit rows. Run daily from cron."""
        db = SessionLocal()
        cutoff = utcnow() - timedelta(days=S.EVENT_RETENTION_DAYS)
        n1 = db.query(Event).filter(Event.created_at < cutoff).delete()
        n2 = db.query(RateEvent).filter(RateEvent.created_at < utcnow() - timedelta(days=2)).delete()
        db.commit()
        click.echo(f"Removed {n1} old events and {n2} rate-limit rows.")

    return app


app = create_app() if os.environ.get("CATALOG_NO_AUTOAPP") != "1" else None

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
