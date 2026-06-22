from app.db import log_request


async def log_api_request(
    tenant_id: int,
    prompt_hash: str,
    hit: bool,
    latency_ms: int,
    model: str,
    response_body: dict,
):
    usage = response_body.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")

    await log_request(
        tenant_id=tenant_id,
        prompt_hash=prompt_hash,
        hit=hit,
        latency_ms=latency_ms,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )