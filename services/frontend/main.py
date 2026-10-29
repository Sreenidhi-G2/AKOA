from fastapi import FastAPI, HTTPException
import time
import requests


from prometheus_fastapi_instrumentator import Instrumentator


app = FastAPI()

Instrumentator().instrument(app).expose(app)


BACKEND_URL = "http://backend:8000"


@app.get("/")
def home():
    return {
        "service": "frontend",
        "status": "running"
    }


@app.get("/health")
def health():
    return {
        "status": "healthy"
    }


@app.get("/work")
def work():
    time.sleep(0.1)

    return {
        "message": "work completed"
    }


@app.get("/backend")
def call_backend():
    try:
        r = requests.get(f"{BACKEND_URL}/work", timeout=5)
        r.raise_for_status()
        return {
            "service": "frontend",
            "backend_response": r.json()
        }
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"backend unreachable: {e}")