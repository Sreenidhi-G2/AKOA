"""Runtime settings, read from AKOA_* environment variables."""
import os
from dataclasses import dataclass
from pathlib import Path


def _csv(name: str, default: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in os.getenv(name, default).split(",") if p.strip())


@dataclass(frozen=True)
class Settings:
    prometheus_url: str
    namespace: str
    services: tuple[str, ...]  # allowlist: only these deployments are exposed (and interpolated into PromQL)
    environment: str
    cors_origins: tuple[str, ...]
    dependencies_file: Path
    pricing_file: Path
    rate_window: str  # PromQL rate() window for "live" numbers

    # Status thresholds (all explainable, all overridable)
    warn_cpu_pct: float  # CPU usage as % of CPU *request*; above this -> Warning
    idle_cpu_pct: float  # below this AND low traffic -> Underutilized
    idle_req_rate: float  # req/s
    warn_error_rate: float  # fraction, e.g. 0.01 = 1%
    degraded_error_rate: float
    restart_warn_1h: float


def load_settings() -> Settings:
    root = Path(__file__).resolve().parent.parent
    return Settings(
        prometheus_url=os.getenv("AKOA_PROMETHEUS_URL", "http://localhost:9090"),
        namespace=os.getenv("AKOA_NAMESPACE", "default"),
        services=_csv("AKOA_SERVICES", "frontend,backend,db-service"),
        environment=os.getenv("AKOA_ENVIRONMENT", "Development / Minikube"),
        cors_origins=_csv("AKOA_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"),
        dependencies_file=Path(os.getenv("AKOA_DEPENDENCIES_FILE", root / "dependencies.yaml")),
        pricing_file=Path(os.getenv("AKOA_PRICING_FILE", root / "pricing.yaml")),
        rate_window=os.getenv("AKOA_RATE_WINDOW", "1m"),
        warn_cpu_pct=float(os.getenv("AKOA_WARN_CPU_PCT", "100")),
        idle_cpu_pct=float(os.getenv("AKOA_IDLE_CPU_PCT", "10")),
        idle_req_rate=float(os.getenv("AKOA_IDLE_REQ_RATE", "1")),
        warn_error_rate=float(os.getenv("AKOA_WARN_ERROR_RATE", "0.01")),
        degraded_error_rate=float(os.getenv("AKOA_DEGRADED_ERROR_RATE", "0.05")),
        restart_warn_1h=float(os.getenv("AKOA_RESTART_WARN_1H", "3")),
    )
