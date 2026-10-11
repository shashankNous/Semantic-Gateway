from fastapi import FastAPI

from app.routes.proxy import router as proxy_router
from app.routes.stats import router as stats_router
from app.db import init_db_pool, close_db_pool, verify_auth_schema, verify_embedding_dimension
from app.middleware import StatsAccessLogMiddleware, StatsCORSMiddleware

app = FastAPI(title="Semantic LLM Caching Gateway")

# Middleware runs outermost-first, and the last one added is the outermost. CORS
# therefore wraps the access log, so a preflight rejected at the CORS layer is not
# recorded as a stats read.
app.add_middleware(StatsAccessLogMiddleware)
app.add_middleware(StatsCORSMiddleware)


@app.on_event("startup")
async def startup():
    await init_db_pool()
    # Authentication has no plaintext fallback, so a database whose credential
    # backfill has not run would 401 every request, one quiet request at a time.
    # Refuse to come up instead. See app/db.py:verify_auth_schema.
    await verify_auth_schema()
    # Same idea for the semantic cache: a vector width that does not match the
    # embedding model would otherwise surface as an INSERT error on the first miss.
    await verify_embedding_dimension()


@app.on_event("shutdown")
async def shutdown():
    await close_db_pool()


@app.get("/health")
async def health_check():
    return {"status": "ok"}


app.include_router(proxy_router, prefix="/v1")
app.include_router(stats_router, prefix="/v1")
