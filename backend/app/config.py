import os
from pathlib import Path

APP_NAME = "Batchgate"
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DB_FILENAME = "app.db"
EXPORTS_DIRNAME = "audit_exports"
EXPORTS_DIR = DATA_DIR / EXPORTS_DIRNAME
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
