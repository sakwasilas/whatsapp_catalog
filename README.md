# WhatsApp Catalog Maker (v2)

Flask + SQLAlchemy app that lets small businesses in Kenya publish a product catalog and take orders on
WhatsApp. Free: 5 products. Premium: KSh 100 / 30 days via M-Pesa (approved by an admin).

## What is new in v2
- **Password reset & email verification** (signed one-time links, anti-enumeration, rate limited) plus an admin
  "generate reset link" tool to send over WhatsApp when a seller cannot receive email.
- **Order tracking** (the old unused `Order` model is now a real feature): record orders, statuses, totals, search,
  CSV export (Premium).
- **Analytics**: anonymous views, unique visitors, order taps, daily chart, top products (Premium, up to 90 days).
- **Catalog**: instant search, category pills, sort, in-stock filter, product pages, sale badges, top picks,
  share button, Open Graph previews, Premium colour themes and no branding.
- **Seller dashboard**: stats, setup checklist, QR code (SVG/PNG), WhatsApp share, plan box, mobile bottom nav.
- **Admin**: KPIs, signup and revenue charts, pending payments, expiring Premium, seller search/filter,
  seller detail page (grant/revoke Premium, verify email, suspend), reject reasons, CSV export, activity log.
- **Landing page** rebuilt, plus Terms and Privacy pages.
- **Hardening**: DB-backed throttling (works across workers/restarts), POST-only logout, strict CSP (no inline
  scripts), safe redirects, CSV formula-injection guard, images re-encoded to WebP and resized, no default
  database password, ProxyFix support, self-hosted Bootstrap (no CDN).
- Tests (`pytest`) and an `upgrade.py` for existing v1 databases.

## Run locally (zero setup)
```bash
python -m venv venv && source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements-dev.txt
python create.py                                      # creates tables in a local SQLite file
ADMIN_EMAIL=you@example.com ADMIN_PASSWORD='long-password' python create.py   # optional: first admin
python app.py                                         # http://127.0.0.1:5000
pytest                                                # run the tests
```
Without `DATABASE_URL` the app uses SQLite (`dev.db`). Without `MAIL_HOST`, emails (reset / confirmation links)
are printed in the terminal.

## Production (MySQL)
1. `pip install -r requirements.txt`, set the variables in `.env.example` in your host's environment.
2. `python create.py` (creates the database and tables) and create the admin as above.
3. `gunicorn -w 3 -b 127.0.0.1:8000 app:app` behind nginx/Caddy with HTTPS. Set `TRUST_PROXY=1`.
   nginx: `client_max_body_size 6m;` and serve `/static/` directly.
4. Cron, daily: `flask --app app cleanup` (deletes analytics older than 120 days and old throttle rows).
5. Back up `uploads/` together with the database.

## Upgrading an existing v1 database
```bash
python upgrade.py     # adds new columns/tables; drops the empty legacy orders table; marks old users as verified
```
Back up first. Old product images (jpg/png) keep working; new uploads are stored as WebP.

## Structure
```
app.py          app factory, CSRF, security headers, error pages, `cleanup` command
settings.py     constants and environment configuration
connection.py   the only engine / session / Base
models.py       User, Business, Product, Category, Subscription, Payment, Order, Event, RateEvent, AdminLog
core.py         helpers: images, tokens, mail, throttling, plan rules, analytics, decorators
routes/         public.py  auth.py  seller.py  admin.py
templates/      layouts + public/ auth/ seller/ admin/
tests/          pytest suite (21 tests)
```

## Business rules
- Premium comes only from an active premium subscription with a future expiry (`core.active_premium`).
  `core.grant_premium` is the single place it is granted (payment approval, manual grant, future Daraja callback).
- Free plan: first 5 products (oldest) are public; the rest are hidden, never deleted.
- Submitting an M-Pesa code only creates a pending payment. An admin must approve it (POST + CSRF + row lock).
  If email is configured, sellers must confirm their email before submitting a payment.

## Before launch
- Have a lawyer review `templates/public/terms.html` and `privacy.html` (Kenya Data Protection Act).
- Replace the manual M-Pesa check with Daraja STK Push / C2B callbacks when volume grows.
- Analytics days use UTC; visitor counting uses a daily salted hash (no IPs stored).
