import sys
from pathlib import Path

# Make `backend` importable as a package when running pytest from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
