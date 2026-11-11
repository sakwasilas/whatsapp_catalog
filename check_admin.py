from connection import SessionLocal
from models import User

db = SessionLocal()

try:
    user = db.query(User).filter(
        User.email == "sakwahsilas@gmail.com"
    ).first()

    if user:
        print("User found:", user.email)
        print("Role:", user.role)
        print("Active:", user.is_active)
        print("Email verified:", user.email_verified)
    else:
        print("Administrator not found!")

finally:
    SessionLocal.remove()