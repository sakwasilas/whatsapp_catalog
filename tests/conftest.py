import os
import re
import sys
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["APP_ENV"] = "development"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from werkzeug.security import generate_password_hash

import settings as S
from app import create_app
from connection import Base, SessionLocal, engine
import core
from models import User

BROWSER = {"User-Agent": "Mozilla/5.0 (Linux; Android 13) Chrome/120 Mobile Safari/537.36"}


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    for name in ("UPLOAD_ROOT", "PRODUCT_UPLOAD_DIR", "PROFILE_UPLOAD_DIR"):
        pass
    root = tmp_path / "uploads"
    (root / "products").mkdir(parents=True)
    (root / "profiles").mkdir(parents=True)
    monkeypatch.setattr(S, "UPLOAD_ROOT", str(root))
    monkeypatch.setattr(S, "PRODUCT_UPLOAD_DIR", str(root / "products"))
    monkeypatch.setattr(S, "PROFILE_UPLOAD_DIR", str(root / "profiles"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    core.OUTBOX.clear()
    yield
    SessionLocal.remove()


@pytest.fixture
def app():
    return create_app({"TESTING": True})


class Client:
    """Test client that handles CSRF tokens automatically."""
    def __init__(self, app):
        self.c = app.test_client()
        self.app = app

    def token(self):
        self.c.get("/forgot-password", headers=BROWSER)
        with self.c.session_transaction() as s:
            return s["_csrf"]

    def get(self, url, **kw):
        kw.setdefault("headers", BROWSER)
        return self.c.get(url, **kw)

    def post(self, url, data=None, csrf=True, **kw):
        data = dict(data or {})
        if csrf:
            data["csrf_token"] = self.token()
        kw.setdefault("headers", BROWSER)
        return self.c.post(url, data=data, **kw)


@pytest.fixture
def client(app):
    return Client(app)


def register(client, name="Amina Wanjiru", email="amina@example.com", phone="0712345678", password="secret-pass-1"):
    return client.post("/register", {"full_name": name, "email": email, "phone": phone, "password": password,
                                     "confirm_password": password, "terms": "on"})


def make_business(client, name="Amina Fashions"):
    return client.post("/business/profile", {"business_name": name, "whatsapp_number": "0712345678",
                                             "business_category": "Shoes", "location": "Nairobi"})


def add_product(client, name="Sandals", price="1500", **extra):
    return client.post("/products/add", {"product_name": name, "price": price, "is_available": "on", **extra})


@pytest.fixture
def seller(app):
    c = Client(app)
    register(c)
    make_business(c)
    return c


@pytest.fixture
def admin(app):
    db = SessionLocal()
    db.add(User(full_name="Boss", email="boss@example.com", phone=None, role="admin",
                password_hash=generate_password_hash("admin-pass-123"), is_active=True, email_verified=True))
    db.commit()
    c = Client(app)
    r = c.post("/admin/login", {"email": "boss@example.com", "password": "admin-pass-123"})
    assert r.status_code == 302
    return c


def last_link(pattern):
    body = core.OUTBOX[-1]["body"]
    return re.search(pattern, body).group(0)
