"""Load `.env` from exactly one place.

A bare `load_dotenv()` searches upward from the calling file until it finds a
`.env`. Run from a git worktree (`<repo>/.claude/worktrees/<name>/`) or a copied
subfolder, that silently loads some *other* checkout's `.env`, with its real keys
and possibly a remote DATABASE_URL. This never searches: it reads `ENV_FILE` if
that is set, otherwise `<project root>/.env`, and otherwise nothing.

The project root comes from this file's location, not the working directory, so
starting the app from elsewhere does not change which file is read.

`override=False`: anything already in the real environment wins, which is how
Railway injects production config. `PYTHON_DOTENV_DISABLED=1` still turns loading
off entirely (python-dotenv honours it), which the local Ollama scripts rely on to
read `.env.local` and nothing else.
"""

import os
import pathlib

from dotenv import load_dotenv

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent


def env_file_path(project_root: pathlib.Path | None = None) -> pathlib.Path:
    explicit = os.getenv("ENV_FILE")
    if explicit:
        return pathlib.Path(explicit)
    return (PROJECT_ROOT if project_root is None else project_root) / ".env"


def load_env(project_root: pathlib.Path | None = None) -> pathlib.Path | None:
    """Load the one `.env` this project uses. Returns its path, or None if absent."""
    path = env_file_path(project_root)
    if not path.is_file():
        return None
    load_dotenv(dotenv_path=path, override=False)
    return path
