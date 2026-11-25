from fastapi import FastAPI

from app.config import load_settings

settings = load_settings()  # fail at import on invalid configuration

app = FastAPI(title="Weekly Feedback Tool")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
