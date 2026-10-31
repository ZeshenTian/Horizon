"""Load shell/GitHub secrets, shared local keys, then project defaults."""

import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv as _load_dotenv


def is_placeholder(value: str | None) -> bool:
    return not value or value.strip().lower() in {
        "xxx", "sk-xxx", "your-key-here", "your_key_here", "your_api_key_here",
    } or value.startswith("${")


def load_environment(dotenv_path=None, override: bool = False) -> bool:
    """Real process secrets take precedence over the shared file and local .env.

    HORIZON_GLOBAL_ENV_FILE can override the default shared-key file location.
    Missing shared files are normal on GitHub Actions, which injects Secrets.
    """
    shared = Path(os.environ.get(
        "HORIZON_GLOBAL_ENV_FILE", str(Path.home() / ".config/ai-keys/keys.env")
    )).expanduser()
    if shared.is_file():
        for name, value in dotenv_values(shared).items():
            if value is not None and is_placeholder(os.environ.get(name)):
                os.environ[name] = value
    project_file = dotenv_path or Path(__file__).resolve().parents[1] / ".env"
    return _load_dotenv(project_file, override=override)
