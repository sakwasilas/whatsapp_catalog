from getpass import getpass
from connection import SessionLocal
from models import User
from werkzeug.security import check_password_hash

db = SessionLocal()

try:
    user = db.query(User).filter_by(
        email="sakwahsilas@gmail.com"
    ).first()

    if not user:
        print("User not found.")
    else:
        password = getpass("Enter your admin password: ")

        if check_password_hash(user.password_hash, password):
            print("SUCCESS: Password matches the database.")
        else:
            print("FAILED: Password does not match.")

finally:
    SessionLocal.remove()