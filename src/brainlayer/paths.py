"""Centralized data paths for BrainLayer.

Resolution order:
  1. BRAINLAYER_DB env var (full path to .db file)
  2. ~/.local/share/brainlayer/brainlayer.db (canonical path)
"""

import os
from pathlib import Path

_CANONICAL_DB_PATH = Path.home() / ".local" / "share" / "brainlayer" / "brainlayer.db"


def get_db_path() -> Path:
    """Resolve the BrainLayer database path.

    Checks BRAINLAYER_DB env var first, then uses the canonical path.
    """
    env = os.environ.get("BRAINLAYER_DB")
    if env:
        return Path(env)

    _CANONICAL_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return _CANONICAL_DB_PATH


# Convenience: pre-resolved default for import
DEFAULT_DB_PATH = get_db_path()

# API key for HTTP MCP transport authentication
API_KEY_DIR = Path.home() / ".config" / "brainlayer"
API_KEY_PATH = API_KEY_DIR / "api_key"


def ensure_api_key() -> str:
    """Get or generate the API key for HTTP MCP transport.

    Resolution order:
      1. BRAINLAYER_API_KEY env var (for CI/containers)
      2. ~/.config/brainlayer/api_key file (auto-generated on first use)
    """
    import logging
    import secrets

    logger = logging.getLogger(__name__)

    env_key = os.environ.get("BRAINLAYER_API_KEY")
    if env_key:
        return env_key.strip()

    if API_KEY_PATH.exists():
        content = API_KEY_PATH.read_text().strip()
        if content:
            return content
        # Empty file — remove so we can recreate atomically
        API_KEY_PATH.unlink(missing_ok=True)

    # Generate new key with atomic file creation (O_CREAT | O_EXCL prevents TOCTOU race)
    API_KEY_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = secrets.token_urlsafe(32)
    try:
        fd = os.open(str(API_KEY_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(fd, key.encode())
        os.close(fd)
        logger.info(f"Generated API key at {API_KEY_PATH}")
    except FileExistsError:
        # Another process created it between our unlink and open — read theirs
        key = API_KEY_PATH.read_text().strip()

    return key
