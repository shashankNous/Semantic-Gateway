"""Loading and checking `.env.local`, shared by the local-testing scripts.

    scripts/local_setup.py        bring up the local stack and issue credentials
    scripts/run_local_gateway.py  run the gateway against it
    scripts/local_smoke_test.py   drive the gateway end to end

Two guarantees these scripts lean on:

  * **Only `.env.local` is read.** The app calls `load_dotenv()` at import time,
    which walks *up* from `app/` looking for a `.env`. From a worktree that finds
    the main checkout's `.env`, with its real keys and possibly a remote
    DATABASE_URL, and quietly fills in anything `.env.local` left unset. Setting
    PYTHON_DOTENV_DISABLED turns that off, so the values here are the only ones.
  * **Nothing remote is reachable.** Every URL the gateway or scripts connect to
    must point at this machine, or the script refuses to start.
"""

import os
import pathlib
from urllib.parse import urlparse

from dotenv import dotenv_values

REPO = pathlib.Path(__file__).resolve().parent.parent
#: Default env file. LOCAL_ENV_FILE (relative to the repo) picks another, e.g. the
#: padding configuration in .env.local.padded. Set in the environment, it is
#: inherited by every subprocess, so the setup, the gateway and the smoke test's
#: second gateway all read the same file.
DEFAULT_ENV_FILE = ".env.local"
CREDENTIALS_FILE = REPO / ".local-credentials.json"
COMPOSE_FILE = REPO / "docker-compose.local.yml"

#: Every variable here is a connection the gateway or the scripts make.
URL_VARS = ("DATABASE_URL", "REDIS_URL", "EMBEDDING_BASE_URL", "UPSTREAM_BASE_URL")

#: host.docker.internal is the host machine as seen from inside a container.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "host.docker.internal"})


def env_file_path() -> pathlib.Path:
    return REPO / os.getenv("LOCAL_ENV_FILE", DEFAULT_ENV_FILE)


def load_local_env(path: pathlib.Path | None = None, overrides: dict | None = None) -> dict:
    """Read the local env file, apply overrides, and refuse anything non-local."""
    path = env_file_path() if path is None else path
    if not path.exists():
        raise SystemExit(
            f"{path.name} not found. Copy .env.local.example to {path.name} first."
        )

    env = {k: v for k, v in dotenv_values(path).items() if v is not None}
    env.update(overrides or {})

    for name in URL_VARS:
        value = env.get(name)
        if not value:
            raise SystemExit(f"{name} must be set in {path.name}")
        host = urlparse(value).hostname
        if host not in LOCAL_HOSTS:
            raise SystemExit(
                f"{name} points at {host!r}. Local testing only talks to "
                f"{sorted(LOCAL_HOSTS)}; refusing to continue."
            )

    return env


def expected_native_dim(env: dict) -> int:
    """How many numbers the model should return: its native width, not the column's.

    Resolved by the gateway's own rules, so the padding configuration
    (EMBEDDING_DIM=1536, EMBEDDING_NATIVE_DIM=768) checks for 768.
    """
    from app.services.embeddings import resolve_embedding_config

    return resolve_embedding_config(env)["EMBEDDING_NATIVE_DIM"]


def apply_to_process(env: dict) -> None:
    """Export `env` into os.environ and stop the app reading any `.env`."""
    os.environ.update(env)
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"


def child_env(env: dict) -> dict:
    """Environment for a subprocess: this one, plus `env`, with .env loading off."""
    merged = dict(os.environ)
    merged.update(env)
    merged["PYTHON_DOTENV_DISABLED"] = "1"
    return merged
