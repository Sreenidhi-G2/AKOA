"""A fake Prometheus with canned data shaped like the real cluster."""
from datetime import datetime, timezone

from app.prometheus import PrometheusUnavailable


def vec(label: str, values: dict[str, float]) -> list[dict]:
    return [{"metric": {label: k}, "value": [0, str(v)]} for k, v in values.items()]


PODS = [
    "backend-678d76c547-aaaaa",
    "backend-678d76c547-bbbbb",
    "frontend-5574cb5d64-ccccc",
    "db-service-544cc5b986-ddddd",
    "nginx-69b9cdbbdd-eeeee",  # not in the allowlist: must never show up
]
SVC_PODS = PODS[:4]

# (needle in the PromQL, canned result). First match wins, so order matters.
FLEET_RULES = [
    ("avg_over_time(sum(container_memory", [{"metric": {}, "value": [0, "100"]}]),
    ("avg_over_time(sum(rate(container_cpu", [{"metric": {}, "value": [0, "90"]}]),
    ("avg_over_time(sum(rate(http_requests_total", [{"metric": {}, "value": [0, "28"]}]),
    ("kube_deployment_spec_replicas", vec("deployment", {"backend": 2, "frontend": 1, "db-service": 1, "nginx": 1})),
    ("kube_deployment_status_replicas_available", vec("deployment", {"backend": 2, "frontend": 1, "db-service": 1, "nginx": 1})),
    ("rate(container_cpu_usage_seconds_total", vec("pod", dict(zip(PODS, [60, 60, 150, 2, 1])))),
    ('resource_requests{namespace="default",resource="cpu"}', vec("pod", {p: 100 for p in SVC_PODS})),
    ('resource_limits{namespace="default",resource="cpu"}', []),  # no CPU limits set
    ("container_memory_working_set_bytes", vec("pod", {p: 40 for p in PODS})),
    ('resource_requests{namespace="default",resource="memory"}', vec("pod", {p: 128 for p in SVC_PODS})),
    ('resource_limits{namespace="default",resource="memory"}', []),
    ('status="5xx"', []),  # no errors at all
    ("http_request_duration_seconds_sum", vec("job", {"frontend": 0.5, "backend": 3.15})),
    ("http_request_duration_seconds_count", vec("job", {"frontend": 30, "backend": 30})),
    ("http_requests_total", vec("job", {"frontend": 30, "backend": 30})),  # db-service: no series
    ("max by (job)(up{", vec("job", {"frontend": 1, "backend": 1, "db-service": 1})),
    ("increase(kube_pod_container_status_restarts_total", vec("pod", {p: 0 for p in PODS})),
    ("sum by (pod)(kube_pod_container_status_restarts_total", vec("pod", {p: 1 for p in PODS})),
    ("kube_pod_start_time", vec("pod", dict(zip(PODS, [3600, 3600, 7200, 100, 9999])))),
    ("kube_pod_status_ready", vec("pod", {p: 1 for p in PODS})),
    (
        "kube_pod_container_info",
        [{"metric": {"pod": p, "image": f"akoa-{p.split('-')[0]}:1.0"}, "value": [0, "1"]} for p in PODS],
    ),
]


class FakeProm:
    def __init__(self, rules=FLEET_RULES):
        self.rules = rules
        self.last_success = datetime(2026, 10, 23, 7, 0, tzinfo=timezone.utc)
        self.queries: list[str] = []

    async def query(self, promql: str):
        self.queries.append(promql)
        for needle, result in self.rules:
            if needle in promql:
                return result
        return []

    async def query_range(self, promql: str, start: int, end: int, step: int):
        self.queries.append(promql)
        if "http_request_duration" in promql:
            return []  # simulate a series with no data
        return [{"metric": {}, "values": [[start, "1.0"], [start + step, "2.0"], [start + 2 * step, "NaN"]]}]

    async def ping(self):
        return True

    async def aclose(self):
        pass


class DownProm(FakeProm):
    async def query(self, promql):
        raise PrometheusUnavailable("connection refused")

    async def query_range(self, promql, start, end, step):
        raise PrometheusUnavailable("connection refused")

    async def ping(self):
        return False
