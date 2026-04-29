from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base, Session

engine = create_engine("sqlite:///./selgo.db", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()