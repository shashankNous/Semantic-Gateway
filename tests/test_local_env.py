"""The local-testing helpers: which env file they read, and what width they expect.

scripts/local_smoke_test.py is deliberately not imported here: it applies its env
file to the process at import (see tests/test_collection.py).
"""

import pytest

from scripts import _local_env
from scripts._local_env import expected_native_dim

LOCAL = (
    "DATABASE_URL=postgresql://postgres:localtest@localhost:55432/semanticdb\n"
    "REDIS_URL=redis://localhost:56379/0\n"
    "EMBEDDING_BASE_URL=http://localhost:11434/v1\n"
    "UPSTREAM_BASE_URL=http://localhost:11434/v1\n"
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(_local_env, "REPO", tmp_path)
    monkeypatch.delenv("LOCAL_ENV_FILE", raising=False)
    return tmp_path


def test_the_default_env_file_is_dot_env_local(repo):
    (repo / ".env.local").write_text(LOCAL + "EMBEDDING_DIM=768\n", encoding="utf-8")

    assert _local_env.env_file_path() == repo / ".env.local"
    assert _local_env.load_local_env()["EMBEDDING_DIM"] == "768"


def test_local_env_file_selects_another_file(repo, monkeypatch):
    (repo / ".env.local").write_text(LOCAL + "EMBEDDING_DIM=768\n", encoding="utf-8")
    (repo / ".env.local.padded").write_text(
        LOCAL + "EMBEDDING_DIM=1536\nEMBEDDING_NATIVE_DIM=768\n", encoding="utf-8"
    )
    monkeypatch.setenv("LOCAL_ENV_FILE", ".env.local.padded")

    env = _local_env.load_local_env()

    assert (env["EMBEDDING_DIM"], env["EMBEDDING_NATIVE_DIM"]) == ("1536", "768")


def test_an_alternate_file_is_still_refused_if_it_points_anywhere_remote(repo, monkeypatch):
    (repo / ".env.remote").write_text(
        LOCAL.replace("localhost:55432", "db.example.railway.app:5432"), encoding="utf-8"
    )
    monkeypatch.setenv("LOCAL_ENV_FILE", ".env.remote")

    with pytest.raises(SystemExit, match="DATABASE_URL"):
        _local_env.load_local_env()


@pytest.mark.parametrize(
    "env, expected",
    [
        ({"EMBEDDING_DIM": "768"}, 768),
        ({"EMBEDDING_DIM": "1536", "EMBEDDING_NATIVE_DIM": "768"}, 768),
        ({"EMBEDDING_PROVIDER": "mistral"}, 1024),
    ],
    ids=["column-only", "padded", "preset"],
)
def test_local_setup_checks_the_model_against_its_native_width(env, expected):
    """Not the column width: in the padding config the model returns 768 into 1536."""
    assert expected_native_dim(env) == expected
