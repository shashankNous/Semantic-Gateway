"""Replay a prepared query set against a running gateway, streaming each result.

    python scripts/replay_bitext.py --input replay_set.csv --key sk-... [--limit 20]

Every request is sent the way `is_request_cacheable()` (app/routes/proxy.py) needs
in order to consider it at all: temperature 0, no `stream`, no `tools`.

Ground truth comes off the `X-Cache` response header, which the proxy sets on
*every* response — EXACT_HIT, SEMANTIC_HIT, MISS or UNCACHEABLE — so no outcome is
inferred from a missing header.

Similarity is in no header, so it is read two ways:

  * A nearest-neighbour probe against `cache_entries`, issued *before* each
    request, which is the top candidate the gateway is about to see. This is the
    only way to get a score for a MISS — afterwards the row has been stored and
    would match itself at 1.0 — and it is what makes the sub-threshold histogram
    buckets meaningful rather than empty by construction.
  * `requests.similarity`, the gateway's own recorded value, for accepted hits,
    as a cross-check. The two should agree.

The probe needs an embedding of its own, so this script makes roughly one extra
embedding call per request on top of the gateway's. Both counts are reported at
the end; the probe's share is this script's cost, not the gateway's.

Output streams one line per request as it happens, and is *also* appended to
`--log` for later spot-checking. The terminal is the point: this is meant to be
watched alongside the dashboard.
"""

import argparse
import asyncio
import csv
import os
import pathlib
import sys
import time

import asyncpg
import httpx

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv  # noqa: E402  (path setup must precede the import)

load_dotenv(REPO / ".env")

from app.security import hash_token  # noqa: E402  (same)
from app.services.embeddings import embed_text  # noqa: E402
from app.services.normalization import normalize_query  # noqa: E402

#: Upper bound of each histogram bucket, and its label.
BUCKETS = ((0.84, "<0.84"), (0.92, "0.84-0.92"))
BUCKET_LABELS = ("<0.84", "0.84-0.92", ">0.92", "no candidate")

LOG_FIELDS = (
    "seq", "intent", "utterance", "normalized", "cache_status",
    "candidate_similarity", "candidate_query", "gateway_similarity",
    "latency_ms", "detail",
)


def bucket_for(score) -> str:
    if score is None:
        return "no candidate"
    for upper, label in BUCKETS:
        if score < upper:
            return label
    return ">0.92"


def embedding_literal(embedding: list[float]) -> str:
    return "[" + ",".join(str(value) for value in embedding) + "]"


async def embed_with_retry(text: str, attempts: int = 5):
    """Embed, riding out the transient upstream failures a long replay will hit.

    A single 503 from the embeddings provider killed an earlier run 66 rows in.
    The probe is instrumentation, not the thing under test, so it retries rather
    than ending the replay. Returns None if it never succeeds, which is recorded
    as a probe gap rather than treated as a request outcome.
    """
    delay = 1.0
    for attempt in range(1, attempts + 1):
        try:
            return await embed_text(text)
        except Exception as exc:
            if attempt == attempts:
                print(f"    probe embedding failed after {attempts} attempts: "
                      f"{type(exc).__name__}: {exc}")
                return None
            await asyncio.sleep(delay)
            delay *= 2
    return None


async def probe_top_candidate(conn, tenant_uuid, embedding):
    """The best existing cache entry for this query, as the gateway is about to see it."""
    row = await conn.fetchrow(
        """
        SELECT raw_query, normalized_query, 1 - (embedding <=> $2::vector) AS similarity
        FROM cache_entries
        WHERE tenant_id = $1::uuid
        ORDER BY embedding <=> $2::vector
        LIMIT 1
        """,
        str(tenant_uuid),
        embedding_literal(embedding),
    )
    if row is None:
        return None, None
    return float(row["similarity"]), (row["raw_query"] or row["normalized_query"])


async def logged_similarity(conn, tenant_id):
    """The gateway's own similarity for the row it has just written."""
    return await conn.fetchval(
        "SELECT similarity FROM requests WHERE tenant_id = $1 ORDER BY id DESC LIMIT 1",
        tenant_id,
    )


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="CSV with utterance/intent columns")
    parser.add_argument("--key", required=True, help="tenant proxy key (sk-...)")
    parser.add_argument("--gateway", default="http://localhost:8000")
    parser.add_argument("--limit", type=int, default=0, help="stop after N rows (0 = all)")
    parser.add_argument("--offset", type=int, default=0, help="skip the first N rows")
    parser.add_argument("--delay", type=float, default=0.3, help="seconds between requests")
    parser.add_argument("--log", default="replay_log.csv")
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL", "").replace("@postgres:5432", "@localhost:5433"),
        help="host-reachable Postgres URL, for the similarity probe",
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()

    with open(args.input, encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    rows = rows[args.offset:]
    if args.limit:
        rows = rows[: args.limit]

    conn = await asyncpg.connect(args.database_url)
    try:
        tenant = await conn.fetchrow(
            "SELECT id, uuid, name FROM tenants WHERE api_key_hash = $1", hash_token(args.key)
        )
        if tenant is None:
            print("That proxy key does not match any tenant in this database.")
            return 1

        print(f'Replaying {len(rows)} rows as tenant {tenant["id"]} "{tenant["name"]}"')
        print(f"Gateway {args.gateway}, {args.delay}s between requests\n")

        counts = {"EXACT_HIT": 0, "SEMANTIC_HIT": 0, "MISS": 0, "UNCACHEABLE": 0, "ERROR": 0}
        histogram = dict.fromkeys(BUCKET_LABELS, 0)
        probe_embeddings = 0
        log_rows = []

        # Written and flushed per row rather than at the end: an earlier run died
        # mid-replay and took the whole record with it, and re-running costs real
        # upstream calls.
        log_handle = open(args.log, "w", newline="", encoding="utf-8")
        log_writer = csv.DictWriter(log_handle, fieldnames=LOG_FIELDS)
        log_writer.writeheader()

        async with httpx.AsyncClient(timeout=120.0) as client:
            for index, row in enumerate(rows, start=args.offset + 1):
                utterance = row["utterance"]
                normalized = row.get("normalized") or normalize_query(utterance)

                probe_embeddings += 1
                embedding = await embed_with_retry(normalized)
                if embedding is None:
                    candidate_score, candidate_query = None, None
                else:
                    candidate_score, candidate_query = await probe_top_candidate(
                        conn, tenant["uuid"], embedding
                    )

                detail = ""
                started = time.perf_counter()
                try:
                    response = await client.post(
                        f"{args.gateway}/v1/chat/completions",
                        headers={
                            "Authorization": f"Bearer {args.key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "messages": [{"role": "user", "content": utterance}],
                            "temperature": 0,
                        },
                    )
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    if response.status_code == 200:
                        status = response.headers.get("X-Cache") or "UNKNOWN"
                    else:
                        status = "ERROR"
                        detail = f"  HTTP {response.status_code}: " + response.text[:110].replace("\n", " ")
                except Exception as exc:
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    status = "ERROR"
                    detail = f"  {type(exc).__name__}: {exc}"

                counts[status] = counts.get(status, 0) + 1
                histogram[bucket_for(candidate_score)] += 1

                gateway_similarity = None
                if status in ("SEMANTIC_HIT", "EXACT_HIT"):
                    gateway_similarity = await logged_similarity(conn, tenant["id"])

                # Errors are not outcomes of the cache, so they are excluded from the
                # running rate rather than counted as misses.
                decided = counts["EXACT_HIT"] + counts["SEMANTIC_HIT"] + counts["MISS"] + counts["UNCACHEABLE"]
                hits = counts["EXACT_HIT"] + counts["SEMANTIC_HIT"]
                hit_rate = hits / decided if decided else 0.0

                score_text = f"sim={candidate_score:.3f}" if candidate_score is not None else "sim=  -  "
                matched = ""
                if status == "SEMANTIC_HIT" and candidate_query:
                    matched = f"  <- {candidate_query[:42]!r}"

                print(
                    f"[{index:3d}/{args.offset + len(rows)}] {status:<12s} {score_text} "
                    f"{latency_ms:5d}ms  {utterance[:44]!r:<48s} | hit rate: {hit_rate:5.1%}"
                    f"{matched}{detail}"
                )
                sys.stdout.flush()

                entry = {
                    "seq": index,
                    "intent": row.get("intent", ""),
                    "utterance": utterance,
                    "normalized": normalized,
                    "cache_status": status,
                    "candidate_similarity": candidate_score,
                    "candidate_query": candidate_query,
                    "gateway_similarity": gateway_similarity,
                    "latency_ms": latency_ms,
                    "detail": detail.strip(),
                }
                log_rows.append(entry)
                log_writer.writerow(entry)
                log_handle.flush()

                if args.delay:
                    await asyncio.sleep(args.delay)

        log_handle.close()

        total = sum(counts.values())
        hits = counts["EXACT_HIT"] + counts["SEMANTIC_HIT"]
        decided = total - counts["ERROR"]

        print("\n" + "-" * 76)
        print(f"replayed          {total}")
        for name in ("EXACT_HIT", "SEMANTIC_HIT", "MISS", "UNCACHEABLE", "ERROR"):
            print(f"  {name:<14s}  {counts[name]}")
        if decided:
            print(f"hit rate          {hits / decided:.1%}  (errors excluded)")

        print("\ntop-candidate similarity, all requests:")
        for label in BUCKET_LABELS:
            print(f"  {label:<14s}  {histogram[label]}")

        print("\nupstream calls made by this run:")
        print(f"  gateway embeddings   ~{counts['SEMANTIC_HIT'] + counts['MISS']}"
              "  (one per cacheable request that missed the Redis exact cache)")
        print(f"  gateway completions   {counts['MISS'] + counts['UNCACHEABLE']}"
              "  (one per MISS or UNCACHEABLE)")
        print(f"  probe embeddings      {probe_embeddings}"
              "  (this script's own, not gateway cost)")
        print(f"\nlog written to {args.log}")
    finally:
        await conn.close()

    return 0


sys.exit(asyncio.run(main()))
