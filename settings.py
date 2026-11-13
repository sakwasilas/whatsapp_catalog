"""Central configuration. Everything tunable lives here or in environment variables."""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_ROOT = os.path.join(BASE_DIR, "uploads")
PRODUCT_UPLOAD_DIR = os.path.join(UPLOAD_ROOT, "products")
PROFILE_UPLOAD_DIR = os.path.join(UPLOAD_ROOT, "profiles")

IS_PRODUCTION = os.environ.get("APP_ENV", "development").lower() == "production"

PLATFORM_NAME = os.environ.get("PLATFORM_NAME", "Duka Yangu")
SUPPORT_WHATSAPP = os.environ.get("SUPPORT_WHATSAPP", "")      # e.g. 254712345678
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "")

# Plans
FREE_PRODUCT_LIMIT = 5
PREMIUM_PRICE = int(os.environ.get("PREMIUM_PRICE", "100"))     # KSh
PREMIUM_DAYS = 30
FREE_ANALYTICS_DAYS = 7
PREMIUM_ANALYTICS_DAYS = 90

# Pagination — public catalogue page size (min 1, default 12)
try:
    CATALOG_PAGE_SIZE = max(1, int(os.environ.get("CATALOG_PAGE_SIZE", "12")))
except ValueError:
    CATALOG_PAGE_SIZE = 12

# Uploads
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_REQUEST_BYTES = 6 * 1024 * 1024
PRODUCT_IMAGE_MAX_PX = 1000
LOGO_MAX_PX = 400

# Tokens (seconds)
RESET_TOKEN_MAX_AGE = 60 * 60            # 1 hour
VERIFY_TOKEN_MAX_AGE = 60 * 60 * 24 * 3  # 3 days

# Email (SMTP). If MAIL_HOST is empty, emails are only written to the log.
MAIL_HOST = os.environ.get("MAIL_HOST", "")
MAIL_PORT = int(os.environ.get("MAIL_PORT", "587"))
MAIL_USERNAME = os.environ.get("MAIL_USERNAME", "")
MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD", "")
MAIL_USE_SSL = os.environ.get("MAIL_USE_SSL", "0") == "1"       # port 465
MAIL_FROM = os.environ.get("MAIL_FROM", "") or MAIL_USERNAME or "no-reply@localhost"
MAIL_ENABLED = bool(MAIL_HOST)

# Analytics retention (days) - run `flask --app app cleanup` from cron
EVENT_RETENTION_DAYS = 120

BUSINESS_CATEGORIES = [
    "Clothing & Fashion", "Shoes", "Cosmetics & Beauty", "Food & Drinks",
    "Electronics", "Home & Furniture", "Jewelry & Accessories",
    "Groceries & Retail", "Services", "Other",
]
DEFAULT_PRODUCT_CATEGORIES = ["Clothes", "Shoes", "Beauty", "Food", "Accessories", "Home", "Other"]

# Catalog accent colours (premium sellers can choose)
THEME_COLORS = {
    "green": "#128c7e", "blue": "#1d4ed8", "purple": "#7c3aed", "pink": "#db2777",
    "orange": "#ea580c", "red": "#dc2626", "black": "#1f2937",
}

ORDER_STATUSES = ["new", "confirmed", "delivered", "cancelled"]

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:5000")