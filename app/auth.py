from fastapi import Request, HTTPException
from app.db import get_tenant_by_key


async def verify_api_key(request: Request) -> dict:
    auth_header = request.headers.get("Authorization", "")

    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    api_key = auth_header.removeprefix("Bearer ").strip()
    tenant = await get_tenant_by_key(api_key)

    if tenant is None:
        raise HTTPException(status_code=401, detail="Unknown API key")

    return {"tenant_id": tenant["id"], "name": tenant["name"]}