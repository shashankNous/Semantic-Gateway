"""With none of the provider variables set, the gateway must resolve to production.

Local testing (CLAUDE.md, .env.local.example) points embeddings and completions at
Ollama purely through environment variables. These tests pin the other half of
that contract: an environment that sets none of them gets OpenAI embeddings at
1536 dimensions, Mistral completions, and the 0.92 similarity threshold.

They run in a subprocess with the variables removed and `.env` loading disabled,
because the app modules call load_dotenv() at import time, and an in-process
check would only report whatever this machine's environment happens to hold.
"""

import json
import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent

PROVIDER_VARS = (
    "EMBEDDING_PROVIDER",
    "EMBEDDING_BASE_URL",
    "EMBEDDING_API_KEY",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIM",
    "EMBEDDING_TIMEOUT_SECONDS",
    "UPSTREAM_BASE_URL",
    "UPSTREAM_MODEL",
    "MISTRAL_BASE_URL",
    "SEMANTIC_CACHE_THRESHOLD",
)

PROBE = """
import json
from app.services import embeddings, semantic_cache
from app.routes import proxy
print(json.dumps({
    "embedding_provider": embeddings.EMBEDDING_PROVIDER,
    "embedding_url": embeddings.OPENAI_EMBEDDINGS_URL,
    "embedding_model": embeddings.EMBEDDING_MODEL,
    "embedding_dim": embeddings.EMBEDDING_DIM,
    "embedding_timeout": embeddings.EMBEDDING_TIMEOUT_SECONDS,
    "completion_url": proxy.MISTRAL_BASE_URL,
    "completion_model": proxy.UPSTREAM_MODEL,
    "threshold": semantic_cache.DEFAULT_THRESHOLD,
}))
"""


def resolved_config(**overrides) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in PROVIDER_VARS}
    env["PYTHON_DOTENV_DISABLED"] = "1"
    env.update(overrides)
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_no_provider_variables_means_production_providers():
    assert resolved_config() == {
        "embedding_provider": "openai",
        "embedding_url": "https://api.openai.com/v1/embeddings",
        "embedding_model": "text-embedding-3-small",
        "embedding_dim": 1536,
        # Deliberately lowered from the old hardcoded 30s: a hung embedding service
        # delays every exact-cache miss by this much before degrading to upstream.
        "embedding_timeout": 10.0,
        "completion_url": "https://api.mistral.ai/v1/chat/completions",
        "completion_model": "mistral-small-latest",
        "threshold": 0.92,
    }


def test_the_local_variables_redirect_both_providers():
    config = resolved_config(
        EMBEDDING_BASE_URL="http://localhost:11434/v1/",
        EMBEDDING_MODEL="nomic-embed-text",
        EMBEDDING_DIM="768",
        EMBEDDING_TIMEOUT_SECONDS="45",
        UPSTREAM_BASE_URL="http://localhost:11434/v1",
        UPSTREAM_MODEL="phi3",
        SEMANTIC_CACHE_THRESHOLD="0.85",
    )

    assert config["embedding_url"] == "http://localhost:11434/v1/embeddings"
    assert config["embedding_dim"] == 768
    assert config["embedding_timeout"] == 45.0
    assert config["completion_url"] == "http://localhost:11434/v1/chat/completions"
    assert config["completion_model"] == "phi3"
    assert config["threshold"] == 0.85
