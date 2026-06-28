"""Skill-local .env loader. Reads <skill>/.env into os.environ."""
import os
from pathlib import Path

_loaded = False

def load():
    global _loaded
    if _loaded:
        return
    _loaded = True
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and v and not os.environ.get(k):
            os.environ[k] = v

load()
