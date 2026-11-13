"""SQLAlchemy models. All inherit Base from connection.py."""
from datetime import datetime

from sqlalchemy import (
    Column, Integer, String, Text, Boolean, DateTime, Numeric,
    ForeignKey, Index, UniqueConstraint,
)
from sqlalchemy.orm import relationship

from connection import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    full_name = Column(String(120), nullable=False)
    email = Column(String(190), unique=True, nullable=True, index=True)
    phone = Column(String(20), unique=True, nullable=True, index=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(10), nullable=False, default="seller", index=True)  # seller | admin
    is_active = Column(Boolean, nullable=False, default=True)
    email_verified = Column(Boolean, nullable=False, default=False)
    last_login_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    business = relationship("Business", back_populates="owner", uselist=False,
                            cascade="all, delete-orphan")
    admin_logs = relationship("AdminLog", back_populates="admin")


class Business(Base):
    __tablename__ = "businesses"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, unique=True)
    business_name = Column(String(150), nullable=False)
    slug = Column(String(180), nullable=False, unique=True, index=True)
    description = Column(Text, nullable=True)
    logo = Column(String(255), nullable=True)
    whatsapp_number = Column(String(20), nullable=True)
    location = Column(String(150), nullable=True)
    business_category = Column(String(80), nullable=True)
    instagram = Column(String(60), nullable=True)
    theme = Column(String(12), nullable=False, default="green")     # premium feature
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow,
                        onupdate=datetime.utcnow)

    owner = relationship("User", back_populates="business")
    products = relationship("Product", back_populates="business",
                            cascade="all, delete-orphan", order_by="Product.id")
    categories = relationship("Category", back_populates="business",
                              cascade="all, delete-orphan")
    subscriptions = relationship("Subscription", back_populates="business",
                                 cascade="all, delete-orphan", order_by="Subscription.id")
    payments = relationship("Payment", back_populates="business",
                            cascade="all, delete-orphan")
    orders = relationship("Order", back_populates="business",
                          cascade="all, delete-orphan")
    events = relationship("Event", back_populates="business",
                          cascade="all, delete-orphan")


class ShopBanner(Base):
    """Seller-managed promotional banners shown on the seller's public shop."""
    __tablename__ = "shop_banners"

    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True)
    image = Column(String(255), nullable=False)
    heading = Column(String(120), nullable=False)
    description = Column(String(300), nullable=True)
    button_text = Column(String(40), nullable=True)
    button_link = Column(String(500), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    product_name = Column(String(150), nullable=False)
    description = Column(Text, nullable=True)
    price = Column(Numeric(12, 2), nullable=False, default=0)
    compare_price = Column(Numeric(12, 2), nullable=True)   # old price, shown struck through
    image = Column(String(255), nullable=True)
    category = Column(String(80), nullable=True)
    is_available = Column(Boolean, nullable=False, default=True)
    is_featured = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow,
                        onupdate=datetime.utcnow)

    business = relationship("Business", back_populates="products")

    __table_args__ = (Index("ix_products_business_created", "business_id", "id"),)

    @property
    def on_sale(self):
        return self.compare_price is not None and self.compare_price > self.price

    @property
    def discount_percent(self):
        if not self.on_sale:
            return 0
        return int(round((self.compare_price - self.price) * 100 / self.compare_price))


class Category(Base):
    __tablename__ = "categories"

    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    category_name = Column(String(80), nullable=False)

    business = relationship("Business", back_populates="categories")
    __table_args__ = (UniqueConstraint("business_id", "category_name", name="uq_category_per_business"),)


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    plan_type = Column(String(10), nullable=False, default="free")      # free | premium
    status = Column(String(10), nullable=False, default="active")       # active | expired
    source = Column(String(10), nullable=False, default="payment")      # payment | admin
    start_date = Column(DateTime, nullable=True)
    expiry_date = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    business = relationship("Business", back_populates="subscriptions")


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    amount = Column(Numeric(10, 2), nullable=False)
    payment_method = Column(String(30), nullable=False, default="mpesa")
    transaction_reference = Column(String(30), nullable=False, unique=True)
    status = Column(String(10), nullable=False, default="pending", index=True)  # pending|approved|rejected
    note = Column(String(255), nullable=True)            # rejection reason shown to the seller
    reviewed_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    payment_date = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    business = relationship("Business", back_populates="payments")


class Order(Base):
    """An order the seller records (usually after a WhatsApp conversation)."""
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="SET NULL"), nullable=True)
    product_name = Column(String(150), nullable=False)       # snapshot, survives product deletion
    quantity = Column(Integer, nullable=False, default=1)
    unit_price = Column(Numeric(12, 2), nullable=False, default=0)
    total_amount = Column(Numeric(12, 2), nullable=False, default=0)
    customer_name = Column(String(120), nullable=True)
    customer_phone = Column(String(20), nullable=True)
    notes = Column(String(500), nullable=True)
    status = Column(String(15), nullable=False, default="new", index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow,
                        onupdate=datetime.utcnow)

    business = relationship("Business", back_populates="orders")
    product = relationship("Product")

    __table_args__ = (Index("ix_orders_business_created", "business_id", "created_at"),)


class Event(Base):
    """Anonymous catalog analytics. event_type: view | product_view | order_click | chat_click."""
    __tablename__ = "events"

    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="SET NULL"), nullable=True)
    event_type = Column(String(15), nullable=False)
    visitor = Column(String(16), nullable=True)               # daily salted hash, not reversible
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    business = relationship("Business", back_populates="events")

    __table_args__ = (Index("ix_events_business_created", "business_id", "created_at"),)


class RateEvent(Base):
    """Shared (multi-worker safe) counter for login / reset / signup throttling."""
    __tablename__ = "rate_events"

    id = Column(Integer, primary_key=True)
    bucket = Column(String(160), nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class AdminLog(Base):
    __tablename__ = "admin_logs"

    id = Column(Integer, primary_key=True)
    admin_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    action = Column(String(60), nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    admin = relationship("User", back_populates="admin_logs")
