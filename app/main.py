from fastapi import FastAPI
from app.routes.proxy import router as proxy_router
from app.db import init_db_pool, close_db_pool

app = FastAPI(title="Semantic LLM Caching Gateway")

@app.on_event("startup")
async def startup():
    await init_db_pool()

@app.on_event("shutdown")
async def shutdown():
    await close_db_pool()

@app.get("/health")
async def health_check():
    return {"status": "ok"}

app.include_router(proxy_router, prefix="/v1")