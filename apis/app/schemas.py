"""Response models. These are the contract the React UI codes against."""
from typing import Literal

from pydantic import BaseModel

Status = Literal["Healthy", "Underutilized", "Warning", "Degraded", "Critical", "Unknown"]
TimeRange = Literal["15m", "1h", "6h", "24h", "7d"]


class PodInfo(BaseModel):
    name: str
    ready: bool | None
    cpu_m: float | None
    mem_mib: float | None
    restarts_total: float | None
    restarts_1h: float | None
    uptime_seconds: float | None
    image: str | None


class ServiceSummary(BaseModel):
    name: str
    status: Status
    status_reasons: list[str]
    replicas_desired: float | None
    replicas_ready: float | None
    cpu_m: float | None  # total usage across pods, millicores
    cpu_request_m: float | None
    cpu_limit_m: float | None
    cpu_pct: float | None  # usage as % of CPU REQUEST (can exceed 100)
    cpu_limit_pct: float | None
    mem_mib: float | None
    mem_request_mib: float | None
    mem_limit_mib: float | None
    mem_pct: float | None  # working set as % of memory REQUEST
    req_rate: float | None  # req/s; None = not scraped, 0 = scraped and idle
    error_rate: float | None  # fraction 0..1; None when there is no traffic
    latency_ms: float | None  # mean, not p95 (see README: highr buckets are too coarse)
    restarts_1h: float | None
    uptime_seconds: float | None  # age of the newest pod = time since last (re)deployment
    image: str | None
    metrics_scraped: bool  # is the app's /metrics endpoint UP in Prometheus?


class Dependencies(BaseModel):
    upstream: list[str]  # services this one calls
    downstream: list[str]  # services that call this one
    source: Literal["configured"] = "configured"


class AnalysisWindow(BaseModel):
    minutes: int
    cpu_m_avg: float | None
    cpu_pct_avg: float | None
    req_rate_avg: float | None


class ServiceDetail(ServiceSummary):
    pods: list[PodInfo]
    dependencies: Dependencies
    analysis_window: AnalysisWindow
    data_source: Literal["prometheus"] = "prometheus"
    updated_at: str


class ServicesResponse(BaseModel):
    environment: str
    updated_at: str
    data_source: Literal["prometheus"] = "prometheus"
    services: list[ServiceSummary]


class TelemetrySource(BaseModel):
    name: str
    status: Literal["connected", "degraded", "unreachable"]
    last_success: str | None


class OverviewSummary(BaseModel):
    services: int
    running_replicas: float
    desired_replicas: float
    cpu_used_m: float
    cpu_requested_m: float
    cpu_pct_of_request: float | None  # sum(usage)/sum(request), not a mean of percentages
    mem_used_mib: float
    mem_requested_mib: float
    mem_pct_of_request: float | None
    by_status: dict[str, int]


class OverviewResponse(BaseModel):
    environment: str
    updated_at: str
    summary: OverviewSummary
    services: list[ServiceSummary]
    sources: list[TelemetrySource]


class Point(BaseModel):
    t: int  # unix seconds
    v: float


class SeriesStats(BaseModel):
    min: float | None
    avg: float | None
    max: float | None
    last: float | None


class Series(BaseModel):
    unit: str
    points: list[Point]
    stats: SeriesStats


class MetricsResponse(BaseModel):
    service: str
    range: TimeRange
    step_seconds: int
    start: int
    end: int
    available: bool  # False -> UI shows "No metrics available for this period."
    missing: list[str]  # series with no data in this window
    series: dict[str, Series]
    data_source: Literal["prometheus"] = "prometheus"


class HealthResponse(BaseModel):
    status: Literal["ok"]
    prometheus: Literal["connected", "unreachable"]
    environment: str


# ---------------------------------------------------------------- cost engine
UsageWindow = Literal["10m", "1h", "6h", "24h"]


class FxInfo(BaseModel):
    usd_to_inr: float
    as_of: str
    source: str


class CostAssumptions(BaseModel):
    """Everything a cost figure depends on. Shown next to every number, never hidden."""

    profile: str
    provider: str
    description: str
    currency: Literal["INR", "USD"]
    vcpu_hour: float  # in `currency`, before the simulation multiplier
    gib_hour: float
    hours_per_month: float
    simulation_multiplier: float
    billing_basis: Literal["requested"] = "requested"
    fx: FxInfo | None  # present only when currency is INR
    price_source: str
    price_verified_on: str
    notes: list[str]
    disclaimer: str


class ServiceCost(BaseModel):
    name: str
    available: bool  # False when CPU/memory requests or replica counts are missing
    replicas: float | None
    cpu_request_m_per_replica: float | None
    mem_request_mib_per_replica: float | None
    cost_per_replica_monthly: float | None
    allocated_monthly: float | None  # what the reserved resources cost
    used_monthly: float | None  # same unit rates, applied to average actual usage
    unused_monthly: float | None  # allocated - used (never below 0)
    unused_pct: float | None
    avg_cpu_m: float | None  # average usage over usage_window, all replicas
    avg_mem_mib: float | None


class CostTotals(BaseModel):
    allocated_monthly: float
    used_monthly: float
    unused_monthly: float
    unused_pct: float | None


class CostsResponse(BaseModel):
    updated_at: str
    usage_window: UsageWindow
    assumptions: CostAssumptions
    services: list[ServiceCost]
    totals: CostTotals


class CostEstimate(BaseModel):
    """What-if: arithmetic on current requests, not a prediction or a recommendation."""

    service: str
    replicas_current: float
    replicas_new: int
    cost_per_replica_monthly: float
    monthly_current: float
    monthly_new: float
    monthly_delta: float  # negative = saving
    pct_change: float | None
    assumptions: CostAssumptions
    notes: list[str]


class ProfileInfo(BaseModel):
    name: str
    provider: str
    description: str
    vcpu_hour: float
    gib_hour: float
    verified_on: str
    source: str
    active: bool


class ProfilesResponse(BaseModel):
    currency: Literal["INR", "USD"]
    active: str
    profiles: list[ProfileInfo]
