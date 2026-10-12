"""Run the gateway against the local test stack and Ollama.

    python scripts/run_local_gateway.py
    python scripts/run_local_gateway.py --port=8011 --set EMBEDDING_MODEL=nope
    python scripts/run_local_gateway.py --env-file .env.local.padded

Loads `.env.local` and nothing else (see scripts/_local_env.py for why that needs
saying), refuses any non-local URL, then starts uvicorn. `--set KEY=VALUE` overrides
one variable for this run only; the smoke test uses it to start a second gateway
with a broken embedding model.
"""

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import os  # noqa: E402

from scripts._local_env import REPO, apply_to_process, load_local_env  # noqa: E402


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--env-file", default=None,
                        help="env file relative to the repo (sets LOCAL_ENV_FILE)")
    args = parser.parse_args(argv)

    if args.env_file:
        # Through the environment, so the gateway's own subprocesses agree.
        os.environ["LOCAL_ENV_FILE"] = args.env_file

    overrides = {}
    for item in args.set:
        key, sep, value = item.partition("=")
        if not sep:
            parser.error(f"--set expects KEY=VALUE, got {item!r}")
        overrides[key] = value

    return args.port, overrides


def main(argv=None) -> None:
    port, overrides = parse_args(sys.argv[1:] if argv is None else argv)
    env = load_local_env(overrides=overrides)
    apply_to_process(env)

    port = port or int(env.get("LOCAL_GATEWAY_PORT", "8010"))
    print(
        f"Local gateway on http://127.0.0.1:{port}  "
        f"upstream={env['UPSTREAM_MODEL']}  embeddings={env['EMBEDDING_MODEL']} "
        f"(column {env.get('EMBEDDING_DIM', '1536')}, "
        f"native {env.get('EMBEDDING_NATIVE_DIM') or env.get('EMBEDDING_DIM', '1536')})  "
        f"threshold={env.get('SEMANTIC_CACHE_THRESHOLD', '0.92')}",
        flush=True,
    )

    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=port, app_dir=str(REPO))


if __name__ == "__main__":
    main()
