from fastapi import FastAPI

app = FastAPI(title="Weekly Feedback Tool")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
