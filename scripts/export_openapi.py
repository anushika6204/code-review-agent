"""Regenerate openapi.json:  python scripts/export_openapi.py"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.api import create_app  # noqa: E402
from app.config import Settings  # noqa: E402

app = create_app(Settings(checkpointer="memory", mock_mode=True))
Path("openapi.json").write_text(json.dumps(app.openapi(), indent=2) + "\n")
print("wrote openapi.json")
