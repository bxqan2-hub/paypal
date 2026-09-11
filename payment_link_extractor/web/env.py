from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def load_env_file(path: str | os.PathLike[str], *, required: bool = False) -> bool:
    env_path = Path(path)
    if not env_path.is_file():
        if required:
            raise RuntimeError(f"env file not found: {env_path}")
        return False
    load_dotenv(dotenv_path=env_path, override=False)
    return True


def load_configured_env(env_file: str | None = None) -> Path:
    configured = env_file or os.getenv("OPLL_ENV_FILE", "")
    env_path = Path(configured or ".env").resolve()
    load_env_file(env_path, required=bool(configured))
    return env_path
