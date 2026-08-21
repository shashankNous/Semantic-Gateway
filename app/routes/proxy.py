import os
import time

import httpx

from dotenv import load_dotenv
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse

from app.auth import verify_api_key
from app.services.logging import log_api_request
from app.cache_key import build_cache_key
from app.services.cache import get_cached_response, set_cached_response
from app.services.rate_limit import enforce_rate_limit
from app.services.semantic_cache import find_semantic_match, store_semantic_cache_entry
from app.services.embeddings import embed_text
from app.services.normalization import normalize_query
from app.services.cacheability import is_cacheable

load_dotenv()

router = APIRouter()

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
UPSTREAM_BASE_URL = os.getenv("UPSTREAM_BASE_URL", "https://api.mistral.ai/v1").rstrip("/")
UPSTREAM_MODEL = os.getenv("UPSTREAM_MODEL", "mistral-small-latest")
MISTRAL_BASE_URL = os.getenv(
    "MISTRAL_BASE_URL",
    f"{UPSTREAM_BASE_URL}/chat/completions",
)


def is_request_cacheable(body: dict) -> bool:
    if body.get("stream") is True:
        return False
    if body.get("temperature", 0) != 0:
        return False
    if body.get("tools") or body.get("tool_choice"):
        return False
    return True


def extract_query_text(body: dict) -> str:
    messages = body.get("messages") or []
    for message in reversed(messages):
        if message.get("role") == "user":
            return message.get("content", "")
    return ""


async def call_upstream_llm(body: dict):
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            MISTRAL_BASE_URL,
            json=body,
            headers={
                "Authorization": f"Bearer {MISTRAL_API_KEY}",
                "Content-Type": "application/json",
            },
        )

    try:
        content = response.json()
    except Exception:
        content = {"error": response.text}

    return response.status_code, content


async def log_request_result(
    tenant_id: int,
    prompt_hash: str,
    latency_ms: int,
    model: str,
    response_body: dict,
    cache_status: str,
    similarity: float | None = None,
):
    await log_api_request(
        tenant_id=tenant_id,
        prompt_hash=prompt_hash,
        hit=cache_status in ("EXACT_HIT", "SEMANTIC_HIT"),
        latency_ms=latency_ms,
        model=model,
        response_body=response_body,
        cache_status=cache_status,
        similarity=similarity,
    )


@router.post("/chat/completions")
async def proxy(request: Request):
    if not MISTRAL_API_KEY:
        raise HTTPException(status_code=500, detail="MISTRAL_API_KEY is not configured")

    # 1. Verify tenant API key
    tenant = await verify_api_key(request)
    tenant_id = tenant["tenant_id"]
    tenant_uuid = tenant["tenant_uuid"]

    # 2. Throttle per tenant, before any work that costs money or a round trip
    await enforce_rate_limit(tenant_id)

    start = time.perf_counter()
    body = await request.json()
    body.setdefault("model", UPSTREAM_MODEL)
    model = body.get("model", "unknown")
    cache_key = build_cache_key(body, tenant_id=str(tenant_id))

    raw_query = extract_query_text(body)
    normalized_query = normalize_query(raw_query)
    cacheable = is_request_cacheable(body) and is_cacheable(normalized_query)

    embedding = None

    if cacheable:
        # 3. Check Redis exact-match cache
        cached_response = await get_cached_response(cache_key)

        if cached_response is not None:
            latency_ms = int((time.perf_counter() - start) * 1000)
            print(f"Exact cache HIT: {cache_key} latency={latency_ms}ms")

            await log_request_result(
                tenant_id=tenant_id,
                prompt_hash=cache_key,
                latency_ms=latency_ms,
                model=model,
                response_body=cached_response,
                cache_status="EXACT_HIT",
            )

            return JSONResponse(
                content=cached_response,
                status_code=200,
                headers={"X-Cache": "EXACT_HIT"},
            )

        # 4. Check semantic cache
        embedding = await embed_text(normalized_query)
        semantic_match = await find_semantic_match(
            tenant_uuid,
            embedding,
            normalized_query,
            raw_query=raw_query,
        )

        if semantic_match is not None:
            latency_ms = int((time.perf_counter() - start) * 1000)
            print(f"Semantic cache HIT: {cache_key} similarity={semantic_match['similarity']} latency={latency_ms}ms")

            await log_request_result(
                tenant_id=tenant_id,
                prompt_hash=cache_key,
                latency_ms=latency_ms,
                model=model,
                response_body=semantic_match["response_json"],
                cache_status="SEMANTIC_HIT",
                similarity=semantic_match["similarity"],
            )

            return JSONResponse(
                content=semantic_match["response_json"],
                status_code=200,
                headers={"X-Cache": "SEMANTIC_HIT"},
            )
    else:
        print("Uncacheable request:", cache_key)

    # 5. Call Mistral
    status_code, content = await call_upstream_llm(body)
    latency_ms = int((time.perf_counter() - start) * 1000)
    cache_status = "MISS" if cacheable else "UNCACHEABLE"
    print(f"Returned status={status_code} cache_status={cache_status} latency={latency_ms}ms")

    # 6. Cache successful response and log to Postgres
    if status_code == 200:
        if cacheable:
            await set_cached_response(cache_key, content)
            await store_semantic_cache_entry(
                tenant_uuid,
                normalized_query,
                cache_key,
                content,
                embedding,
                model,
                raw_query=raw_query,
            )

        await log_request_result(
            tenant_id=tenant_id,
            prompt_hash=cache_key,
            latency_ms=latency_ms,
            model=model,
            response_body=content,
            cache_status=cache_status,
        )

    return JSONResponse(
        content=content,
        status_code=status_code,
        headers={"X-Cache": cache_status},
    )
