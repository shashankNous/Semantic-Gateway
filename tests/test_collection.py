"""The test run collects tests/ only, never the operator scripts.

pytest's default file patterns include `*_test.py`, which matches
scripts/local_smoke_test.py. Collecting a file imports it even when it holds no
tests, and that script applies .env.local to os.environ at import. So every test
silently ran under the local Ollama settings (EMBEDDING_DIM=768, the local
DATABASE_URL, PYTHON_DOTENV_DISABLED=1), and on a machine without .env.local,
collection itself failed with SystemExit.
"""

import json
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent

PROBE = """
import json, os, sys
import pytest
before = dict(os.environ)
pytest.main(["--collect-only", "-q", "-p", "no:cacheprovider"])
print(json.dumps({
    "imported_scripts": sorted(m for m in sys.modules if "local_smoke_test" in m
                               or m.endswith("local_setup") or m.endswith("run_local_gateway")),
    "env_changed": sorted(k for k in os.environ if before.get(k) != os.environ[k]),
}))
"""


def test_a_bare_pytest_run_imports_no_operator_script_and_leaves_the_env_alone():
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    report = json.loads(result.stdout.strip().splitlines()[-1])

    assert report["imported_scripts"] == []
    assert report["env_changed"] == []
