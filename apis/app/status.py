"""Service health status: a pure function so every status is explainable and testable.

States (spec): Healthy, Underutilized, Warning, Degraded, Critical, Unknown.
Precedence: Unknown > Critical > Degraded > Warning > Underutilized > Healthy.
Returns (status, reasons) - the reasons are shown in the UI, never hidden.
"""
from .config import Settings


def compute_status(
    *,
    desired: float | None,
    ready: float | None,
    cpu_pct: float | None,
    req_rate: float | None,
    error_rate: float | None,
    restarts_1h: float | None,
    cfg: Settings,
) -> tuple[str, list[str]]:
    if desired is None or ready is None:
        return "Unknown", ["No replica data from kube-state-metrics"]

    if desired > 0 and ready == 0:
        return "Critical", [f"0 of {desired:g} replicas ready"]

    if ready < desired:
        return "Degraded", [f"{ready:g} of {desired:g} replicas ready"]

    if error_rate is not None and error_rate >= cfg.degraded_error_rate:
        return "Degraded", [f"Error rate {error_rate:.1%} (threshold {cfg.degraded_error_rate:.0%})"]

    warnings: list[str] = []
    if error_rate is not None and error_rate >= cfg.warn_error_rate:
        warnings.append(f"Error rate {error_rate:.1%} (threshold {cfg.warn_error_rate:.0%})")
    if cpu_pct is not None and cpu_pct >= cfg.warn_cpu_pct:
        warnings.append(f"CPU at {cpu_pct:.0f}% of request (threshold {cfg.warn_cpu_pct:.0f}%)")
    if restarts_1h is not None and restarts_1h >= cfg.restart_warn_1h:
        warnings.append(f"{restarts_1h:.0f} container restarts in the last hour")
    if warnings:
        return "Warning", warnings

    # Needs both signals: unknown traffic means we cannot call a service idle
    if (
        cpu_pct is not None
        and req_rate is not None
        and cpu_pct < cfg.idle_cpu_pct
        and req_rate < cfg.idle_req_rate
    ):
        return "Underutilized", [
            f"CPU {cpu_pct:.0f}% of request (< {cfg.idle_cpu_pct:.0f}%) and "
            f"{req_rate:.2f} req/s (< {cfg.idle_req_rate:g})"
        ]

    return "Healthy", []
