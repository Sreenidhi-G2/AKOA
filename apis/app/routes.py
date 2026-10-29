import asyncio

from fastapi import APIRouter, Query, Request

from . import cost as cost_engine
from . import dependencies as deps_cfg
from . import telemetry
from .config import Settings
from .errors import ApiError
from .pricing import Pricing, Profile
from .prometheus import PrometheusClient
from .schemas import (
    Dependencies,
    CostEstimate,
    CostsResponse,
    HealthResponse,
    MetricsResponse,
    OverviewResponse,
    OverviewSummary,
    ProfilesResponse,
    ServiceDetail,
    ServicesResponse,
    TelemetrySource,
    TimeRange,
    UsageWindow,
)

router = APIRouter(prefix="/api")


def _prom(request: Request) -> PrometheusClient:
    return request.app.state.prom


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _pricing(request: Request) -> Pricing:
    return request.app.state.pricing


def _profile(request: Request, name: str | None) -> Profile:
    try:
        return _pricing(request).profile(name)
    except KeyError:
        raise ApiError(404, "profile_not_found", f"Pricing profile '{name}' is not configured.") from None


def _require_service(s: Settings, name: str) -> None:
    # Allowlist check: also what keeps `name` safe to interpolate into PromQL.
    if name not in s.services:
        raise ApiError(404, "service_not_found", f"Service '{name}' is not monitored by AKOA.")


@router.get("/health", response_model=HealthResponse, tags=["system"])
async def health(request: Request):
    ok = await _prom(request).ping()
    return HealthResponse(
        status="ok", prometheus="connected" if ok else "unreachable", environment=_settings(request).environment
    )


@router.get("/overview", response_model=OverviewResponse, tags=["overview"])
async def overview(request: Request):
    """Facts only: no recommendations here (Overview = what is happening)."""
    s, prom = _settings(request), _prom(request)
    fleet = await telemetry.collect_fleet(prom, s)
    svcs = list(fleet.services.values())

    def total(attr: str) -> float:
        return sum(getattr(x, attr) or 0.0 for x in svcs)

    cpu_used, cpu_req = total("cpu_m"), total("cpu_request_m")
    mem_used, mem_req = total("mem_mib"), total("mem_request_mib")
    by_status: dict[str, int] = {}
    for x in svcs:
        by_status[x.status] = by_status.get(x.status, 0) + 1

    have_replica_data = any(x.replicas_desired is not None for x in svcs)
    last = prom.last_success.isoformat(timespec="seconds") if prom.last_success else None
    return OverviewResponse(
        environment=s.environment,
        updated_at=telemetry.now_iso(),
        summary=OverviewSummary(
            services=len(svcs),
            running_replicas=total("replicas_ready"),
            desired_replicas=total("replicas_desired"),
            cpu_used_m=round(cpu_used, 2),
            cpu_requested_m=cpu_req,
            cpu_pct_of_request=(cpu_used / cpu_req * 100) if cpu_req else None,
            mem_used_mib=round(mem_used, 2),
            mem_requested_mib=mem_req,
            mem_pct_of_request=(mem_used / mem_req * 100) if mem_req else None,
            by_status=by_status,
        ),
        services=svcs,
        sources=[
            TelemetrySource(name="Prometheus", status="connected", last_success=last),
            TelemetrySource(
                name="kube-state-metrics",
                status="connected" if have_replica_data else "degraded",
                last_success=last,
            ),
        ],
    )


@router.get("/services", response_model=ServicesResponse, tags=["services"])
async def list_services(request: Request):
    s = _settings(request)
    fleet = await telemetry.collect_fleet(_prom(request), s)
    return ServicesResponse(
        environment=s.environment, updated_at=telemetry.now_iso(), services=list(fleet.services.values())
    )


@router.get("/services/{service}", response_model=ServiceDetail, tags=["services"])
async def service_detail(service: str, request: Request):
    s, prom = _settings(request), _prom(request)
    _require_service(s, service)
    fleet = await telemetry.collect_fleet(prom, s)
    summary = fleet.services[service]
    window = await telemetry.collect_analysis_window(prom, s, service, summary.cpu_request_m)
    deps = deps_cfg.load(s)[service]
    return ServiceDetail(
        **summary.model_dump(),
        pods=fleet.pods[service],
        dependencies=Dependencies(**deps),
        analysis_window=window,
        updated_at=telemetry.now_iso(),
    )


@router.get("/metrics/{service}", response_model=MetricsResponse, tags=["metrics"])
async def service_metrics(service: str, request: Request, range: TimeRange = Query("1h")):
    s = _settings(request)
    _require_service(s, service)
    return await telemetry.collect_series(_prom(request), s, service, range)


# ---------------------------------------------------------------- costs
@router.get("/costs/profiles", response_model=ProfilesResponse, tags=["costs"])
async def cost_profiles(request: Request):
    return cost_engine.profiles(_pricing(request))


@router.get("/costs", response_model=CostsResponse, tags=["costs"])
async def costs(
    request: Request,
    usage_window: UsageWindow = Query("1h", description="Window for average actual usage"),
    profile: str | None = Query(None, description="Pricing profile; defaults to the active one"),
):
    s, prom, pricing = _settings(request), _prom(request), _pricing(request)
    p = _profile(request, profile)
    fleet, usage = await telemetry_pair(prom, s, usage_window)
    return cost_engine.build_costs(fleet, usage, pricing, p, usage_window)


@router.get("/costs/estimate", response_model=CostEstimate, tags=["costs"])
async def cost_estimate(
    request: Request,
    service: str,
    replicas: int = Query(..., ge=0, le=100),
    profile: str | None = Query(None),
):
    s, prom = _settings(request), _prom(request)
    _require_service(s, service)
    p = _profile(request, profile)
    fleet = await telemetry.collect_fleet(prom, s)
    return cost_engine.estimate_replicas(fleet, service, replicas, _pricing(request), p)


async def telemetry_pair(prom: PrometheusClient, s: Settings, window: str):
    return await asyncio.gather(telemetry.collect_fleet(prom, s), cost_engine.collect_usage(prom, s, window))
