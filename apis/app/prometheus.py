"""Thin async Prometheus HTTP client. Raises PrometheusUnavailable on any failure."""
import math
from datetime import datetime, timezone

import httpx


class PrometheusUnavailable(Exception):
    pass


class PrometheusClient:
    def __init__(self, base_url: str, timeout: float = 10.0, transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport)
        self.last_success: datetime | None = None

    async def _get(self, path: str, params: dict) -> dict:
        try:
            r = await self._http.get(path, params=params)
            r.raise_for_status()
            body = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise PrometheusUnavailable(str(e)) from e
        if body.get("status") != "success":
            raise PrometheusUnavailable(body.get("error", "query failed"))
        self.last_success = datetime.now(timezone.utc)
        return body["data"]

    async def query(self, promql: str) -> list[dict]:
        return (await self._get("/api/v1/query", {"query": promql}))["result"]

    async def query_range(self, promql: str, start: int, end: int, step: int) -> list[dict]:
        data = await self._get("/api/v1/query_range", {"query": promql, "start": start, "end": end, "step": step})
        return data["result"]

    async def ping(self) -> bool:
        try:
            await self.query("1")
            return True
        except PrometheusUnavailable:
            return False

    async def aclose(self) -> None:
        await self._http.aclose()


def vector(result: list[dict], label: str) -> dict[str, float]:
    """Instant-vector result -> {label_value: float}, skipping NaN/Inf."""
    out: dict[str, float] = {}
    for r in result:
        try:
            v = float(r["value"][1])
        except (KeyError, IndexError, ValueError, TypeError):
            continue
        if math.isnan(v) or math.isinf(v):
            continue
        out[r.get("metric", {}).get(label, "")] = v
    return out


def scalar(result: list[dict]) -> float | None:
    vals = vector(result, "")
    return next(iter(vals.values()), None)


def points(result: list[dict]) -> list[tuple[int, float]]:
    """Range-vector result -> [(unix_ts, value)] for the first series, skipping NaN/Inf."""
    if not result:
        return []
    out = []
    for ts, raw in result[0].get("values", []):
        try:
            v = float(raw)
        except (ValueError, TypeError):
            continue
        if math.isnan(v) or math.isinf(v):
            continue
        out.append((int(float(ts)), v))
    return out
