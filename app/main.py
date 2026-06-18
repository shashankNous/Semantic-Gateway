from fastapi import FastAPI
from app.routes.proxy import router as proxy_router

app = FastAPI(title = "Semantic LLM Caching Gateway")

@app.get("/health")
async def health_check():
    return{"status": "ok"}

app.include_router(proxy_router, prefix= "/v1")