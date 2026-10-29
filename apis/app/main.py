from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import errors
from . import pricing as pricing_mod
from .config import Settings, load_settings
from .prometheus import PrometheusClient
from .routes import router


def create_app(
    settings: Settings | None = None,
    prom: PrometheusClient | None = None,
    pricing: pricing_mod.Pricing | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    pricing = pricing or pricing_mod.load(settings.pricing_file)  # fails fast on a bad pricing.yaml
    owns_client = prom is None
    prom = prom or PrometheusClient(settings.prometheus_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        if owns_client:
            await prom.aclose()

    app = FastAPI(title="AKOA API", version="0.1.0", description="Read-only infrastructure telemetry API", lifespan=lifespan)
    app.state.settings = settings
    app.state.prom = prom
    app.state.pricing = pricing
    app.add_middleware(
        CORSMiddleware, allow_origins=list(settings.cors_origins), allow_methods=["GET"], allow_headers=["*"]
    )
    errors.install(app)
    app.include_router(router)
    return app


app = create_app()
