"""`.env` is loaded from one explicit path and never searched for upward.

python-dotenv's bare `load_dotenv()` walks up from the calling file until it finds
a `.env`. From a git worktree (`<repo>/.claude/worktrees/<name>/`) that is the main
checkout's `.env`, so a run that was meant to be isolated silently picked up real
keys and possibly a remote DATABASE_URL.
"""

import os

import pytest

from app.env import load_env

MARKER = "SG_TEST_ENV_MARKER"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(MARKER, raising=False)
    monkeypatch.delenv("ENV_FILE", raising=False)
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    yield
    os.environ.pop(MARKER, None)


def test_a_dotenv_in_a_parent_directory_is_not_loaded(tmp_path):
    (tmp_path / ".env").write_text(f"{MARKER}=from-parent\n", encoding="utf-8")
    project = tmp_path / "worktrees" / "project"
    project.mkdir(parents=True)

    loaded = load_env(project_root=project)

    assert loaded is None
    assert MARKER not in os.environ


def test_the_project_root_dotenv_is_loaded(tmp_path):
    (tmp_path / ".env").write_text(f"{MARKER}=from-root\n", encoding="utf-8")

    loaded = load_env(project_root=tmp_path)

    assert loaded == tmp_path / ".env"
    assert os.environ[MARKER] == "from-root"


def test_env_file_selects_an_explicit_path(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(f"{MARKER}=from-root\n", encoding="utf-8")
    other = tmp_path / "elsewhere.env"
    other.write_text(f"{MARKER}=from-env-file\n", encoding="utf-8")
    monkeypatch.setenv("ENV_FILE", str(other))

    load_env(project_root=tmp_path)

    assert os.environ[MARKER] == "from-env-file"


def test_real_environment_variables_win(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(f"{MARKER}=from-file\n", encoding="utf-8")
    monkeypatch.setenv(MARKER, "from-environment")

    load_env(project_root=tmp_path)

    assert os.environ[MARKER] == "from-environment"


def test_the_default_root_is_the_repository_not_the_working_directory(tmp_path, monkeypatch):
    """Computed from app/env.py's own location, so `cd` elsewhere changes nothing."""
    import pathlib

    import app.env

    monkeypatch.chdir(tmp_path)

    assert app.env.PROJECT_ROOT == pathlib.Path(app.env.__file__).resolve().parent.parent


def test_python_dotenv_disabled_still_wins(tmp_path, monkeypatch):
    """The local scripts set this so only .env.local is ever read."""
    (tmp_path / ".env").write_text(f"{MARKER}=from-root\n", encoding="utf-8")
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")

    load_env(project_root=tmp_path)

    assert MARKER not in os.environ


def test_no_module_calls_load_dotenv_directly():
    """Every app module goes through app.env.load_env, which never searches upward."""
    import pathlib

    app_dir = pathlib.Path(__file__).resolve().parent.parent / "app"
    offenders = [
        str(path.relative_to(app_dir.parent))
        for path in app_dir.rglob("*.py")
        if path.name != "env.py" and "load_dotenv(" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_the_env_file_is_loaded_before_any_module_reads_its_settings(tmp_path):
    """Modules read settings at import time, so .env must be loaded before them.

    Run in a subprocess: importing app.main here would reuse modules this test
    session already imported, with whatever environment they saw then.
    """
    import pathlib
    import subprocess
    import sys

    env_file = tmp_path / "test.env"
    env_file.write_text(
        "EMBEDDING_MODEL=model-from-env-file\nEMBEDDING_DIM=768\nSEMANTIC_CACHE_THRESHOLD=0.5\n",
        encoding="utf-8",
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("EMBEDDING_MODEL", "EMBEDDING_DIM", "SEMANTIC_CACHE_THRESHOLD",
                     "PYTHON_DOTENV_DISABLED")
    }
    env["ENV_FILE"] = str(env_file)
    probe = (
        "import app.main\n"
        "from app.services import embeddings, semantic_cache\n"
        "print(embeddings.EMBEDDING_MODEL, embeddings.EMBEDDING_DIM, semantic_cache.DEFAULT_THRESHOLD)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=pathlib.Path(__file__).resolve().parent.parent,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.split() == ["model-from-env-file", "768", "0.5"]
