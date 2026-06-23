import hashlib
import json


def build_cache_key(body: dict, tenant_id: str) -> str:
    cache_input = {
        "tenant_id": tenant_id,
        "model": body.get("model"),
        "messages": body.get("messages"),
        "temperature": body.get("temperature", 0),
        "max_tokens": body.get("max_tokens"),
    }

    raw = json.dumps(cache_input, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()

    return f"tenant:{tenant_id}:chat:{digest}"
