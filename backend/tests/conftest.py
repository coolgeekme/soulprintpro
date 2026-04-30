"""Shared pytest setup: load backend .env so unit tests can directly call
helpers that read os.environ (BRAVE_API_KEY, EMERGENT_LLM_KEY, MONGO_URL, etc.).

Also ensures /app/backend is on sys.path for `import server` / `import brave_search`.
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path("/app/backend")
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# override=False so existing env (e.g. in CI) wins
load_dotenv(BACKEND_DIR / ".env", override=False)
