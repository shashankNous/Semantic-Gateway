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

load_dotenv()

router = APIRouter()

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
UPSTREAM_BASE_URL = os.getenv("UPSTREAM_BASE_URL", "https://api.mistral.ai/v1").rstrip("/")
UPSTREAM_MODEL = os.getenv("UPSTREAM_MODEL", "mistral-small-latest")
MISTRAL_BASE_URL = os.getenv(
    "MISTRAL_BASE_URL",
    f"{UPSTREAM_BASE_URL}/chat/completions",
)


def is_cacheable(body: dict) -> bool:
    if body.get("stream") is True:
        return False
    if body.get("temperature", 0) != 0:
        return False
    if body.get("tools") or body.get("tool_choice"):
        return False
    return True


@router.post("/chat/completions")
async def proxy(request: Request):
    if not MISTRAL_API_KEY:
        raise HTTPException(status_code=500, detail="MISTRAL_API_KEY is not configured")

    # 1. Verify tenant API key
    tenant = await verify_api_key(request)
    tenant_id = tenant["tenant_id"]

    start = time.perf_counter()
    body = await request.json()
    body.setdefault("model", UPSTREAM_MODEL)
    cache_key = build_cache_key(body, tenant_id=str(tenant_id))
    model = body.get("model", "unknown")

    # 2. Check Redis cache
    if is_cacheable(body):
        cached_response = await get_cached_response(cache_key)

        if cached_response is not None:
            latency_ms = int((time.perf_counter() - start) * 1000)
            print(f"Cache HIT: {cache_key} latency={latency_ms}ms")

            await log_api_request(
                tenant_id=tenant_id,
                prompt_hash=cache_key,
                hit=True,
                latency_ms=latency_ms,
                model=model,
                response_body=cached_response,
            )

            return JSONResponse(
                content=cached_response,
                status_code=200,
                headers={"X-Cache": "HIT"},
            )

    print("Cache MISS:", cache_key)

    # 3. Call Mistral
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

    # 4. Cache successful response
    if response.status_code == 200 and is_cacheable(body):
        await set_cached_response(cache_key, content)

    latency_ms = int((time.perf_counter() - start) * 1000)
    print(f"Returned status={response.status_code} latency={latency_ms}ms")

    # 5. Log to Postgres
    if response.status_code == 200:
        await log_api_request(
            tenant_id=tenant_id,
            prompt_hash=cache_key,
            hit=False,
            latency_ms=latency_ms,
            model=model,
            response_body=content,
        )

    return JSONResponse(
        content=content,
        status_code=response.status_code,
        headers={"X-Cache": "MISS"},
    )
