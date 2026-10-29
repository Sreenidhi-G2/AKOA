"""Consistent, user-safe error bodies: {"error": {"code", "message", ...}}. Never a stack trace."""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .prometheus import PrometheusUnavailable


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}})

    @app.exception_handler(PrometheusUnavailable)
    async def _prom_down(request: Request, _: PrometheusUnavailable):
        last = request.app.state.prom.last_success
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "telemetry_unavailable",
                    "message": "AKOA cannot retrieve telemetry from Prometheus.",
                    "status": "Telemetry degraded",
                    "last_successful_update": last.isoformat(timespec="seconds") if last else None,
                }
            },
        )
