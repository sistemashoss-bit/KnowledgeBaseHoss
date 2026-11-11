from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.config import settings
from app import timing

engine = create_engine(settings.sqlalchemy_url, pool_pre_ping=True)
timing.instrument_engine(engine)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
