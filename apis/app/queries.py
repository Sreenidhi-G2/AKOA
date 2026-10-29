"""All PromQL lives here.

Service names are interpolated into queries, so callers MUST validate them against
Settings.services first (the routes do). Never pass user input in unvalidated.
"""
from .config import Settings


def _ns(s: Settings) -> str:
    return f'namespace="{s.namespace}"'


def pod_regex(svc: str) -> str:
    # Deployment pods are named <deployment>-<replicaset-hash>-<suffix>
    return f"{svc}-[a-z0-9]+-[a-z0-9]+"


def fleet(s: Settings) -> dict[str, str]:
    """One instant query per metric, covering every service at once."""
    ns, w = _ns(s), s.rate_window
    http = f'{ns},handler!="/metrics"'  # exclude Prometheus' own scrapes from "traffic"
    return {
        "desired": f"sum by (deployment)(kube_deployment_spec_replicas{{{ns}}})",
        "ready": f"sum by (deployment)(kube_deployment_status_replicas_available{{{ns}}})",
        "cpu_m": f'sum by (pod)(rate(container_cpu_usage_seconds_total{{{ns},container!=""}}[{w}]))*1000',
        "cpu_req_m": f'sum by (pod)(kube_pod_container_resource_requests{{{ns},resource="cpu"}})*1000',
        "cpu_lim_m": f'sum by (pod)(kube_pod_container_resource_limits{{{ns},resource="cpu"}})*1000',
        "mem_mib": f'sum by (pod)(container_memory_working_set_bytes{{{ns},container!=""}})/1024/1024',
        "mem_req_mib": f'sum by (pod)(kube_pod_container_resource_requests{{{ns},resource="memory"}})/1024/1024',
        "mem_lim_mib": f'sum by (pod)(kube_pod_container_resource_limits{{{ns},resource="memory"}})/1024/1024',
        "req_rate": f"sum by (job)(rate(http_requests_total{{{http}}}[{w}]))",
        "err_rate": f'sum by (job)(rate(http_requests_total{{{http},status="5xx"}}[{w}]))',
        "lat_sum": f"sum by (job)(rate(http_request_duration_seconds_sum{{{http}}}[{w}]))",
        "lat_cnt": f"sum by (job)(rate(http_request_duration_seconds_count{{{http}}}[{w}]))",
        "up": f"max by (job)(up{{{ns}}})",
        "restarts_1h": f"sum by (pod)(increase(kube_pod_container_status_restarts_total{{{ns}}}[1h]))",
        "restarts_total": f"sum by (pod)(kube_pod_container_status_restarts_total{{{ns}}})",
        "uptime": f"time() - min by (pod)(kube_pod_start_time{{{ns}}})",
        "pod_ready": f'sum by (pod)(kube_pod_status_ready{{{ns},condition="true"}})',
        "image": f"kube_pod_container_info{{{ns}}}",
    }


def analysis_window(s: Settings, svc: str, minutes: int = 10) -> dict[str, str]:
    """Averages over AKOA's decision window (spec: live numbers vs. 10-minute analysis window)."""
    ns, w = _ns(s), s.rate_window
    pods = f'pod=~"{pod_regex(svc)}"'
    return {
        "cpu_m_avg": f'avg_over_time(sum(rate(container_cpu_usage_seconds_total{{{ns},{pods},container!=""}}[{w}]))[{minutes}m:30s])*1000',
        "req_rate_avg": f'avg_over_time(sum(rate(http_requests_total{{{ns},job="{svc}",handler!="/metrics"}}[{w}]))[{minutes}m:30s])',
    }


def usage_avg(s: Settings, svc: str, window: str) -> dict[str, str]:
    """Average total usage of one service over `window` (10m|1h|6h|24h).

    Aggregates across pods BEFORE averaging, so pods replaced mid-window don't double count.
    """
    ns = _ns(s)
    pods = f'pod=~"{pod_regex(svc)}"'
    return {
        "cpu_m": f'avg_over_time(sum(rate(container_cpu_usage_seconds_total{{{ns},{pods},container!=""}}[{s.rate_window}]))[{window}:1m])*1000',
        "mem_mib": f'avg_over_time(sum(container_memory_working_set_bytes{{{ns},{pods},container!=""}})[{window}:1m])/1024/1024',
    }


def series(s: Settings, svc: str, window_s: int) -> dict[str, tuple[str, str]]:
    """Range queries for one service: name -> (promql, unit).

    window_s is the rate() window; the route sets it to max(60, step) so each plotted
    point is an average over its own step (no misleading instantaneous samples on 7d).
    """
    ns, w = _ns(s), f"{window_s}s"
    pods = f'pod=~"{pod_regex(svc)}"'
    http = f'{ns},job="{svc}",handler!="/metrics"'
    return {
        "cpu_m": (f'sum(rate(container_cpu_usage_seconds_total{{{ns},{pods},container!=""}}[{w}]))*1000', "m"),
        "cpu_request_m": (f'sum(kube_pod_container_resource_requests{{{ns},{pods},resource="cpu"}})*1000', "m"),
        "cpu_limit_m": (f'sum(kube_pod_container_resource_limits{{{ns},{pods},resource="cpu"}})*1000', "m"),
        "mem_mib": (f'sum(container_memory_working_set_bytes{{{ns},{pods},container!=""}})/1024/1024', "MiB"),
        "mem_request_mib": (f'sum(kube_pod_container_resource_requests{{{ns},{pods},resource="memory"}})/1024/1024', "MiB"),
        "req_rate": (f"sum(rate(http_requests_total{{{http}}}[{w}]))", "req/s"),
        # (5xx or 0) / total: 0 when there is traffic but no errors, no point when there is no traffic
        "error_rate": (
            f'(sum(rate(http_requests_total{{{http},status="5xx"}}[{w}])) or vector(0))'
            f"/sum(rate(http_requests_total{{{http}}}[{w}]))",
            "ratio",
        ),
        "latency_ms": (
            f"sum(rate(http_request_duration_seconds_sum{{{http}}}[{w}]))"
            f"/sum(rate(http_request_duration_seconds_count{{{http}}}[{w}]))*1000",
            "ms",
        ),
        "replicas": (f'sum(kube_deployment_status_replicas_available{{{ns},deployment="{svc}"}})', "replicas"),
    }
