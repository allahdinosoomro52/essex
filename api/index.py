from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1] / "essex_property_worker"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.main import app as fastapi_app

app = fastapi_app
