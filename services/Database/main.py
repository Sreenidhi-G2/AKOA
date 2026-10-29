from fastapi import FastAPI
import time
from prometheus_fastapi_instrumentator import Instrumentator



app = FastAPI()
Instrumentator().instrument(app).expose(app)

@app.get("/")
def home():
    return {
        "service": "Database",
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
