from sqlalchemy import text
from connection import engine

try:
    with engine.connect() as conn:
        result = conn.execute(text("SELECT DATABASE()"))
        print("Database connected successfully!")
        print("Database name:", result.scalar())

except Exception as e:
    print("Connection failed:")
    print(e)