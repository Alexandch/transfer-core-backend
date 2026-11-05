from app.catalog import Catalog
from app.config import settings
from app.db import SessionLocal
from app.service import seed

if __name__ == "__main__":
    with SessionLocal.begin() as session:
        seed(session, Catalog(settings().data_dir))
