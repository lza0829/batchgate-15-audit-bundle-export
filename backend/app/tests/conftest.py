import sys
from pathlib import Path

# Make `app` importable when pytest is invoked from the backend directory.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
