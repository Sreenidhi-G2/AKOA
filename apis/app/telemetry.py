"""Turns raw Prometheus results into service snapshots. No HTTP framework code here."""
import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from . import queries
from .config import Settings
from .prometheus import PrometheusClient, points, scalar, vector
from .schemas import AnalysisWindow, PodInfo, Point, Series, SeriesStats, ServiceSummary
from .status import compute_status

_POD_RE = re.compile(r"^(?P<svc>.+)-[a-z0-9]+-[a-z0-9]+$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def service_of_pod(pod: str, services: tuple[str, ...]) -> str | None:
    m = _POD_RE.match(pod)
    name = m.group("svc") if m else None
    return name if name in services else None


def _sum_by_service(per_pod: dict[str, float], services: tuple[str, ...]) -> dict[str, float]:
    out: dict[str, float] = {}
    for pod, v in per_pod.items():
        svc = service_of_pod(pod, services)
        if svc:
            out[svc] = out.get(svc, 0.0) + v
    return out


def _min_by_service(per_pod: dict[str, float], services: tuple[str, ...]) -> dict[str, float]:
    out: dict[str, float] = {}
    for pod, v in per_pod.items():
        svc = service_of_pod(pod, services)
        if svc:
            out[svc] = min(out.get(svc, v), v)
    return out


def _pct(used: float | None, ref: float | None) -> float | None:
    if used is None or not ref:
        return None
    return used / ref * 100


@dataclass
class Fleet:
    services: dict[str, ServiceSummary]
    pods: dict[str, list[PodInfo]]


async def collect_fleet(prom: PrometheusClient, s: Settings) -> Fleet:
    qs = queries.fleet(s)
    results = dict(zip(qs.keys(), await asyncio.gather(*(prom.query(q) for q in qs.values()))))

    desired = vector(results["desired"], "deployment")
    ready = vector(results["ready"], "deployment")
    cpu_pod = vector(results["cpu_m"], "pod")
    mem_pod = vector(results["mem_mib"], "pod")
    restarts_1h_pod = vector(results["restarts_1h"], "pod")
    restarts_total_pod = vector(results["restarts_total"], "pod")
    uptime_pod = vector(results["uptime"], "pod")
    ready_pod = vector(results["pod_ready"], "pod")
    cpu_req = _sum_by_service(vector(results["cpu_req_m"], "pod"), s.services)
    cpu_lim = _sum_by_service(vector(results["cpu_lim_m"], "pod"), s.services)
    mem_req = _sum_by_service(vector(results["mem_req_mib"], "pod"), s.services)
    mem_lim = _sum_by_service(vector(results["mem_lim_mib"], "pod"), s.services)
    cpu = _sum_by_service(cpu_pod, s.services)
    mem = _sum_by_service(mem_pod, s.services)
    restarts = _sum_by_service(restarts_1h_pod, s.services)
    uptime = _min_by_service(uptime_pod, s.services)
    req_rate = vector(results["req_rate"], "job")
    err_rate = vector(results["err_rate"], "job")
    lat_sum = vector(results["lat_sum"], "job")
    lat_cnt = vector(results["lat_cnt"], "job")
    up = vector(results["up"], "job")

    images: dict[str, str] = {}
    for r in results["image"]:
        pod = r.get("metric", {}).get("pod", "")
        img = r.get("metric", {}).get("image")
        if img:
            images[pod] = img

    pods: dict[str, list[PodInfo]] = {svc: [] for svc in s.services}
    for pod in sorted(set(cpu_pod) | set(mem_pod) | set(uptime_pod) | set(images)):
        svc = service_of_pod(pod, s.services)
        if svc:
            pods[svc].append(
                PodInfo(
                    name=pod,
                    ready=(ready_pod[pod] >= 1) if pod in ready_pod else None,
                    cpu_m=cpu_pod.get(pod),
                    mem_mib=mem_pod.get(pod),
                    restarts_total=restarts_total_pod.get(pod),
                    restarts_1h=restarts_1h_pod.get(pod),
                    uptime_seconds=uptime_pod.get(pod),
                    image=images.get(pod),
                )
            )

    services: dict[str, ServiceSummary] = {}
    for svc in s.services:
        scraped = up.get(svc) == 1
        # No http_requests_total series: idle if the target is being scraped, unknown if not.
        rate = req_rate.get(svc, 0.0 if scraped else None)
        err = (err_rate.get(svc, 0.0) / rate) if rate else None
        lat = (lat_sum[svc] / lat_cnt[svc] * 1000) if lat_cnt.get(svc) else None
        cpu_pct = _pct(cpu.get(svc), cpu_req.get(svc))
        status, reasons = compute_status(
            desired=desired.get(svc),
            ready=ready.get(svc),
            cpu_pct=cpu_pct,
            req_rate=rate,
            error_rate=err,
            restarts_1h=restarts.get(svc),
            cfg=s,
        )
        svc_images = sorted({p.image for p in pods[svc] if p.image})
        services[svc] = ServiceSummary(
            name=svc,
            status=status,
            status_reasons=reasons,
            replicas_desired=desired.get(svc),
            replicas_ready=ready.get(svc),
            cpu_m=cpu.get(svc),
            cpu_request_m=cpu_req.get(svc),
            cpu_limit_m=cpu_lim.get(svc),
            cpu_pct=cpu_pct,
            cpu_limit_pct=_pct(cpu.get(svc), cpu_lim.get(svc)),
            mem_mib=mem.get(svc),
            mem_request_mib=mem_req.get(svc),
            mem_limit_mib=mem_lim.get(svc),
            mem_pct=_pct(mem.get(svc), mem_req.get(svc)),
            req_rate=rate,
            error_rate=err,
            latency_ms=lat,
            restarts_1h=restarts.get(svc),
            uptime_seconds=uptime.get(svc),
            image=svc_images[0] if svc_images else None,
            metrics_scraped=scraped,
        )
    return Fleet(services=services, pods=pods)


async def collect_analysis_window(
    prom: PrometheusClient, s: Settings, svc: str, cpu_request_m: float | None, minutes: int = 10
) -> AnalysisWindow:
    qs = queries.analysis_window(s, svc, minutes)
    cpu_r, req_r = await asyncio.gather(prom.query(qs["cpu_m_avg"]), prom.query(qs["req_rate_avg"]))
    cpu_avg = scalar(cpu_r)
    return AnalysisWindow(
        minutes=minutes,
        cpu_m_avg=cpu_avg,
        cpu_pct_avg=_pct(cpu_avg, cpu_request_m),
        req_rate_avg=scalar(req_r),
    )


# range -> (seconds, step seconds)
RANGES = {"15m": (900, 15), "1h": (3600, 30), "6h": (21600, 120), "24h": (86400, 300), "7d": (604800, 1800)}


async def collect_series(prom: PrometheusClient, s: Settings, svc: str, range_key: str) -> dict:
    span, step = RANGES[range_key]
    end = int(datetime.now(timezone.utc).timestamp())
    end -= end % step  # align to the step so repeated polls return stable timestamps
    start = end - span
    qs = queries.series(s, svc, window_s=max(60, step))
    raw = await asyncio.gather(*(prom.query_range(q, start, end, step) for q, _unit in qs.values()))

    out: dict[str, Series] = {}
    missing: list[str] = []
    for (name, (_q, unit)), result in zip(qs.items(), raw):
        pts = points(result)
        if not pts:
            missing.append(name)
        vals = [v for _, v in pts]
        out[name] = Series(
            unit=unit,
            points=[Point(t=t, v=round(v, 4)) for t, v in pts],
            stats=SeriesStats(
                min=round(min(vals), 4) if vals else None,
                avg=round(sum(vals) / len(vals), 4) if vals else None,
                max=round(max(vals), 4) if vals else None,
                last=round(vals[-1], 4) if vals else None,
            ),
        )
    return {
        "service": svc,
        "range": range_key,
        "step_seconds": step,
        "start": start,
        "end": end,
        # request/limit lines are reference data; "available" means the live signals exist
        "available": any(out[k].points for k in ("cpu_m", "mem_mib", "req_rate", "replicas")),
        "missing": missing,
        "series": out,
    }
