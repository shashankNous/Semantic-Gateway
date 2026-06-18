import os
import time
import httpx

from dotenv import load_dotenv
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse

from app.services.cache import get_cached_response, set_cached_response
from app.cache_key import build_cache_key

load_dotenv()

router = APIRouter()

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
MISTRAL_BASE_URL = os.getenv(
    "MISTRAL_BASE_URL",
    "https://api.mistral.ai/v1/chat/completions",
)


def is_cacheable(body: dict) -> bool:
    if body.get("stream") is True:
        return False

    if body.get("temperature", 1) != 0:
        return False

    if body.get("tools") or body.get("tool_choice"):
        return False

    return True


@router.post("/chat/completions")
async def proxy(request: Request):
    if not MISTRAL_API_KEY:
        raise HTTPException(status_code=500, detail="MISTRAL_API_KEY is not configured")

    start = time.perf_counter()
    body = await request.json()

    # 1. Build cache key from the request body
    cache_key = build_cache_key(body)

    # 2. Check Redis before calling Mistral
    if is_cacheable(body):
        cached_response = await get_cached_response(cache_key)

        if cached_response is not None:
            latency_ms = int((time.perf_counter() - start) * 1000)
            print(f"Cache HIT: {cache_key} latency={latency_ms}ms")

            return JSONResponse(
                content=cached_response,
                status_code=200,
                headers={"X-Cache": "HIT"},
            )

    print("Cache MISS:", cache_key)
    print("Incoming model:", body.get("model"))

    # 3. Only call Mistral if Redis did not have the response
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

    # 4. Save successful response into Redis
    if response.status_code == 200 and is_cacheable(body):
        await set_cached_response(cache_key, content)

    latency_ms = int((time.perf_counter() - start) * 1000)
    print(f"Returned status={response.status_code} latency={latency_ms}ms")

    return JSONResponse(
        content=content,
        status_code=response.status_code,
        headers={"X-Cache": "MISS"},
    )