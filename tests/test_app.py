import io
import re

from PIL import Image

import core
import settings as S
from connection import SessionLocal
from models import Business, Event, Order, Payment, Product, User
from conftest import BROWSER, Client, add_product, last_link, make_business, register


def png_bytes(size=(1600, 1200), color=(200, 30, 30)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    buf.seek(0)
    return buf


# ----------------------------------------------------------------- public
def test_landing_and_legal_pages(client):
    for url in ("/", "/terms", "/privacy", "/login", "/register", "/forgot-password", "/healthz", "/robots.txt", "/sitemap.xml"):
        assert client.get(url).status_code == 200, url
    assert client.get("/shop/nope").status_code == 404


def test_csrf_required(client):
    r = client.post("/register", {"full_name": "X"}, csrf=False)
    assert r.status_code == 400


def test_logout_is_post_only(seller):
    assert seller.get("/logout").status_code == 405
    assert seller.post("/logout").status_code == 302
    assert seller.get("/dashboard").status_code == 302


# ----------------------------------------------------------------- auth
def test_register_validation_and_duplicates(client):
    r = client.post("/register", {"full_name": "A", "email": "bad", "phone": "1", "password": "x", "confirm_password": "y"})
    assert r.status_code == 200 and b"valid email" in r.data
    assert register(client).status_code == 302
    other = Client(client.app)
    r = register(other, name="Other Person")
    assert b"already exists" in r.data


def test_login_throttle_and_next(client):
    register(client)
    client.post("/logout")
    for _ in range(5):
        r = client.post("/login", {"identifier": "amina@example.com", "password": "wrong"})
        assert b"Incorrect" in r.data
    r = client.post("/login", {"identifier": "amina@example.com", "password": "secret-pass-1"})
    assert r.status_code == 429


def test_login_with_phone_and_safe_next(client):
    register(client)
    client.post("/logout")
    r = client.post("/login", {"identifier": "0712 345 678", "password": "secret-pass-1", "next": "//evil.com"})
    assert r.status_code == 302 and "evil.com" not in r.headers["Location"]


def test_password_reset_flow(client):
    register(client)
    client.post("/logout")
    core.OUTBOX.clear()
    # unknown email: same response, no mail
    r = client.post("/forgot-password", {"email": "ghost@example.com"}, follow_redirects=True)
    assert b"If an account exists" in r.data and not core.OUTBOX
    r = client.post("/forgot-password", {"email": "amina@example.com"}, follow_redirects=True)
    assert b"If an account exists" in r.data and len(core.OUTBOX) == 1
    link = last_link(r"/reset-password/[^\s]+")
    assert client.get(link).status_code == 200
    r = client.post(link, {"password": "brand-new-pass", "confirm_password": "different"})
    assert b"do not match" in r.data
    r = client.post(link, {"password": "brand-new-pass", "confirm_password": "brand-new-pass"})
    assert r.status_code == 302
    # link works exactly once
    assert client.get(link).status_code == 302
    assert client.post("/login", {"identifier": "amina@example.com", "password": "secret-pass-1"}).status_code == 200
    assert client.post("/login", {"identifier": "amina@example.com", "password": "brand-new-pass"}).status_code == 302
    assert client.get("/reset-password/garbage").status_code == 302


def test_email_verification(client):
    register(client)
    link = last_link(r"/verify-email/[^\s]+")
    db = SessionLocal()
    assert db.query(User).one().email_verified is False
    assert client.get(link).status_code == 302
    db.expire_all()
    assert db.query(User).one().email_verified is True
    assert client.get("/verify-email/bad").status_code == 302


# ----------------------------------------------------------------- products & plan limits
def test_free_limit_and_public_catalog(seller):
    for i in range(5):
        assert add_product(seller, f"Item {i}", "100").status_code == 302
    r = add_product(seller, "Item 6", "100")
    assert r.status_code == 302 and "/subscription" in r.headers["Location"]
    assert SessionLocal().query(Product).count() == 5
    page = seller.get("/shop/amina-fashions")
    assert page.status_code == 200 and b"Item 0" in page.data and b"Catalog made with" in page.data


def test_product_validation_and_sale_price(seller):
    r = add_product(seller, "A", "abc")
    assert b"Product name must be" in r.data and b"valid price" in r.data
    r = add_product(seller, "Shirt", "1000", compare_price="500")
    assert b"old price must be higher" in r.data
    add_product(seller, "Shirt", "1000", compare_price="1250")
    page = seller.get("/shop/amina-fashions")
    assert b"-20%" in page.data


def test_image_upload_is_resized_and_validated(seller):
    r = seller.post("/products/add", {"product_name": "Photo", "price": "10",
                                      "image": (png_bytes(), "pic.png")}, content_type="multipart/form-data")
    assert r.status_code == 302
    p = SessionLocal().query(Product).one()
    assert p.image.endswith(".webp")
    with Image.open(S.UPLOAD_ROOT + "/" + p.image) as im:
        assert max(im.size) <= S.PRODUCT_IMAGE_MAX_PX
    # a text file renamed to .png is rejected
    r = seller.post("/products/add", {"product_name": "Fake", "price": "10",
                                      "image": (io.BytesIO(b"not an image at all"), "x.png")},
                    content_type="multipart/form-data")
    assert b"not a valid" in r.data
    assert SessionLocal().query(Product).count() == 1


def test_ownership_isolation(app, seller):
    add_product(seller)
    pid = SessionLocal().query(Product).one().id
    other = Client(app)
    register(other, name="Other Seller", email="o@example.com", phone="0722000000")
    make_business(other, "Other Shop")
    assert other.get(f"/products/edit/{pid}").status_code == 404
    assert other.post(f"/products/delete/{pid}").status_code == 404
    assert other.post(f"/products/{pid}/toggle").status_code == 404


def test_toggle_and_duplicate(seller):
    add_product(seller)
    p = SessionLocal().query(Product).one()
    seller.post(f"/products/{p.id}/toggle")
    SessionLocal().expire_all()
    assert SessionLocal().query(Product).one().is_available is False
    seller.post(f"/products/{p.id}/duplicate")
    assert SessionLocal().query(Product).count() == 2


# ----------------------------------------------------------------- analytics
def test_analytics_counts_real_visitors_only(seller):
    add_product(seller)
    pid = SessionLocal().query(Product).one().id
    visitor = Client(seller.app)
    visitor.get("/shop/amina-fashions")
    visitor.get(f"/shop/amina-fashions/p/{pid}")
    r = visitor.get(f"/go/amina-fashions/{pid}")
    assert r.status_code == 302 and r.headers["Location"].startswith("https://wa.me/254712345678?text=")
    r = visitor.get("/go/amina-fashions")
    assert r.status_code == 302
    # bots and the owner are ignored
    visitor.c.get("/shop/amina-fashions", headers={"User-Agent": "Googlebot/2.1"})
    visitor.c.get("/shop/amina-fashions", headers={"User-Agent": "WhatsApp/2.23"})
    seller.get("/shop/amina-fashions")
    types = sorted(e.event_type for e in SessionLocal().query(Event).all())
    assert types == ["chat_click", "order_click", "product_view", "view"]
    data = core.analytics_summary(SessionLocal(), SessionLocal().query(Business).one().id, 7)
    assert data["views"] == 1 and data["clicks"] == 2 and data["visitors"] == 1
    assert seller.get("/analytics").status_code == 200 and seller.get("/dashboard").status_code == 200


def test_sold_out_product_does_not_redirect_to_whatsapp(seller):
    seller.post("/products/add", {"product_name": "Gone", "price": "5"})   # no is_available => sold out
    pid = SessionLocal().query(Product).one().id
    r = Client(seller.app).get(f"/go/amina-fashions/{pid}")
    assert "wa.me" not in r.headers["Location"]


# ----------------------------------------------------------------- orders
def test_order_lifecycle(app, seller):
    add_product(seller, "Sandals", "1500")
    pid = SessionLocal().query(Product).one().id
    r = seller.post("/orders/new", {"product_id": pid, "quantity": "3", "unit_price": "", "customer_name": "Jane",
                                    "customer_phone": "0700111222", "status": "new"})
    assert r.status_code == 302
    o = SessionLocal().query(Order).one()
    assert o.total_amount == 4500 and o.customer_phone == "254700111222" and o.product_name == "Sandals"
    seller.post(f"/orders/{o.id}/status", {"status": "delivered"})
    SessionLocal().expire_all()
    assert SessionLocal().query(Order).one().status == "delivered"
    assert seller.get("/orders?status=delivered").status_code == 200
    assert seller.post(f"/orders/{o.id}/status", {"status": "bogus"}).status_code == 400
    other = Client(app)
    register(other, name="Other", email="o@example.com", phone="0722000000"); make_business(other, "Other Shop")
    assert other.get(f"/orders/{o.id}/edit").status_code == 404
    assert other.post(f"/orders/{o.id}/delete").status_code == 404
    # free plan cannot export
    assert "/subscription" in seller.get("/orders/export.csv").headers["Location"]
    # deleting the product keeps the order
    seller.post(f"/products/delete/{pid}")
    assert SessionLocal().query(Order).one().product_name == "Sandals"


# ----------------------------------------------------------------- payments & admin
def test_payment_flow_approve_reject_and_premium_features(app, seller, admin):
    seller.post("/payment/submit", {"transaction_reference": "bad"})
    assert SessionLocal().query(Payment).count() == 0
    seller.post("/payment/submit", {"transaction_reference": "shk7xy12ab"})
    pay = SessionLocal().query(Payment).one()
    assert pay.status == "pending" and pay.transaction_reference == "SHK7XY12AB"
    assert seller.get("/orders/export.csv").status_code == 302       # still free
    r = admin.post(f"/admin/payment/approve/{pay.id}")
    assert r.status_code == 302
    SessionLocal().expire_all()
    biz = SessionLocal().query(Business).one()
    assert core.active_premium(SessionLocal(), biz.id) is not None
    # double approval is a no-op
    admin.post(f"/admin/payment/approve/{pay.id}")
    from models import Subscription
    assert SessionLocal().query(Subscription).filter(Subscription.plan_type == "premium").count() == 1
    # premium unlocks: unlimited products, csv, analytics range, no branding
    for i in range(7):
        assert "/subscription" not in add_product(seller, f"P{i}").headers["Location"]
    assert seller.get("/orders/export.csv").mimetype == "text/csv"
    assert seller.get("/analytics?days=90").status_code == 200
    assert b"Catalog made with" not in Client(app).get("/shop/amina-fashions").data
    # a second payment gets rejected with a reason the seller can see
    seller.post("/payment/submit", {"transaction_reference": "ZZZ1234567"})
    pay2 = SessionLocal().query(Payment).filter(Payment.status == "pending").one()
    admin.post(f"/admin/payment/reject/{pay2.id}", {"note": "Amount too low"})
    assert b"Amount too low" in seller.get("/subscription").data
    # duplicate code is refused
    assert b"already been submitted" in seller.post("/payment/submit", {"transaction_reference": "SHK7XY12AB"},
                                                   follow_redirects=True).data


def test_seller_cannot_reach_admin(seller):
    for url in ("/admin/dashboard", "/admin/users", "/admin/payments", "/admin/activity"):
        assert seller.get(url).status_code in (302, 403)
    assert seller.post("/admin/payment/approve/1").status_code in (302, 403)


def test_admin_pages_grant_revoke_and_reset_link(app, seller, admin):
    uid = SessionLocal().query(User).filter(User.role == "seller").one().id
    for url in ("/admin/dashboard", "/admin/users", "/admin/users?filter=free&q=amina", f"/admin/users/{uid}",
                "/admin/payments", "/admin/payments/export.csv", "/admin/activity"):
        assert admin.get(url).status_code == 200, url
    admin.post(f"/admin/users/{uid}/grant", {"days": "10", "reason": "promo"})
    assert core.active_premium(SessionLocal(), SessionLocal().query(Business).one().id)
    assert b"/shop/amina-fashions" in admin.get(f"/admin/users/{uid}").data
    admin.post(f"/admin/users/{uid}/revoke")
    SessionLocal().expire_all()
    assert core.active_premium(SessionLocal(), SessionLocal().query(Business).one().id) is None
    r = admin.post(f"/admin/users/{uid}/reset-link")
    assert r.status_code == 200 and b"/reset-password/" in r.data
    admin.post(f"/admin/users/{uid}/toggle")
    assert Client(app).post("/login", {"identifier": "amina@example.com", "password": "secret-pass-1"}).status_code == 200


# ----------------------------------------------------------------- account
def test_account_change_password_and_delete(app, seller):
    add_product(seller)
    r = seller.post("/account", {"action": "password", "current_password": "wrong", "new_password": "newer-pass-9",
                                 "confirm_password": "newer-pass-9"})
    assert b"incorrect" in r.data
    seller.post("/account", {"action": "password", "current_password": "secret-pass-1",
                             "new_password": "newer-pass-9", "confirm_password": "newer-pass-9"})
    # email change needs the password and resets verification
    r = seller.post("/account", {"action": "profile", "full_name": "Amina W", "email": "new@example.com", "phone": "0712345678"})
    assert b"current password" in r.data
    seller.post("/account", {"action": "profile", "full_name": "Amina W", "email": "new@example.com",
                             "phone": "0712345678", "current_password": "newer-pass-9"})
    assert SessionLocal().query(User).one().email == "new@example.com"
    r = seller.post("/account", {"action": "delete", "current_password": "newer-pass-9", "confirm_text": "nope"})
    assert SessionLocal().query(User).count() == 1
    seller.post("/account", {"action": "delete", "current_password": "newer-pass-9", "confirm_text": "DELETE"})
    assert SessionLocal().query(User).count() == 0 and SessionLocal().query(Product).count() == 0


def test_qr_code(seller):
    assert seller.get("/dashboard/qr.svg").mimetype == "image/svg+xml"
    assert seller.get("/dashboard/qr.png").mimetype == "image/png"
    assert seller.get("/dashboard/qr.gif").status_code == 404
