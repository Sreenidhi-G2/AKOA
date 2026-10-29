import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app
from tests.fakes import DownProm, FakeProm


@pytest.fixture
def client():
    return TestClient(create_app(load_settings(), FakeProm()))


def by_name(services):
    return {s["name"]: s for s in services}


def test_services_statuses_and_allowlist(client):
    body = client.get("/api/services").json()
    svcs = by_name(body["services"])
    assert set(svcs) == {"frontend", "backend", "db-service"}  # nginx excluded
    assert svcs["frontend"]["status"] == "Warning"  # 150m used vs 100m request
    assert svcs["backend"]["status"] == "Healthy"
    assert svcs["db-service"]["status"] == "Underutilized"


def test_service_numbers(client):
    svcs = by_name(client.get("/api/services").json()["services"])
    be, db = svcs["backend"], svcs["db-service"]
    assert be["cpu_m"] == 120 and be["cpu_request_m"] == 200 and be["cpu_pct"] == 60
    assert be["replicas_ready"] == 2 and be["latency_ms"] == pytest.approx(105)
    assert be["cpu_limit_m"] is None and be["cpu_limit_pct"] is None  # no limits -> null, not 0
    assert be["error_rate"] == 0.0
    # scraped but no http series = idle (0), and no traffic means no error rate
    assert db["req_rate"] == 0.0 and db["error_rate"] is None and db["metrics_scraped"] is True
    assert be["image"] == "akoa-backend:1.0"
    assert be["uptime_seconds"] == 3600


def test_overview_is_facts_only_and_sums_not_averages(client):
    body = client.get("/api/overview").json()
    s = body["summary"]
    assert s["services"] == 3 and s["running_replicas"] == 4
    assert s["cpu_used_m"] == 272 and s["cpu_requested_m"] == 400
    assert s["cpu_pct_of_request"] == pytest.approx(68)
    assert s["by_status"] == {"Warning": 1, "Healthy": 1, "Underutilized": 1}
    assert {x["name"] for x in body["sources"]} == {"Prometheus", "kube-state-metrics"}
    assert not any("recommend" in k for k in body)  # no AI content on Overview


def test_service_detail(client):
    d = client.get("/api/services/backend").json()
    assert [p["name"] for p in d["pods"]] == ["backend-678d76c547-aaaaa", "backend-678d76c547-bbbbb"]
    assert d["dependencies"] == {"upstream": [], "downstream": ["frontend"], "source": "configured"}
    assert d["analysis_window"]["minutes"] == 10
    assert d["analysis_window"]["cpu_pct_avg"] == pytest.approx(45)  # 90m avg / 200m request
    assert client.get("/api/services/frontend").json()["dependencies"]["upstream"] == ["backend"]


@pytest.mark.parametrize("name", ["nginx", "nope", "evil%22%7D", "backend%7Cx"])
def test_unknown_service_is_404_and_never_reaches_promql(name):
    prom = FakeProm()
    c = TestClient(create_app(load_settings(), prom))
    for path in (f"/api/services/{name}", f"/api/metrics/{name}"):
        r = c.get(path)
        assert r.status_code == 404 and r.json()["error"]["code"] == "service_not_found"
    assert prom.queries == []


def test_metrics_series_and_missing(client):
    m = client.get("/api/metrics/backend?range=1h").json()
    assert m["range"] == "1h" and m["step_seconds"] == 30 and m["available"] is True
    cpu = m["series"]["cpu_m"]
    assert [p["v"] for p in cpu["points"]] == [1.0, 2.0]  # NaN dropped
    assert cpu["stats"] == {"min": 1.0, "avg": 1.5, "max": 2.0, "last": 2.0}
    assert "latency_ms" in m["missing"] and m["series"]["latency_ms"]["points"] == []


def test_metrics_unavailable_flag():
    class Empty(FakeProm):  # the base fake always returns range data, so override it
        async def query_range(self, *a, **k):
            return []

    c = TestClient(create_app(load_settings(), Empty()))
    m = c.get("/api/metrics/db-service?range=15m").json()
    assert m["available"] is False and len(m["missing"]) == len(m["series"])


@pytest.mark.parametrize("rng, step", [("15m", 15), ("1h", 30), ("6h", 120), ("24h", 300), ("7d", 1800)])
def test_ranges(client, rng, step):
    assert client.get(f"/api/metrics/backend?range={rng}").json()["step_seconds"] == step


def test_bad_range_is_422(client):
    assert client.get("/api/metrics/backend?range=3y").status_code == 422


def test_prometheus_down_gives_503_with_last_update():
    c = TestClient(create_app(load_settings(), DownProm()))
    for path in ("/api/services", "/api/overview", "/api/services/backend", "/api/metrics/backend"):
        r = c.get(path)
        assert r.status_code == 503
        err = r.json()["error"]
        assert err["code"] == "telemetry_unavailable" and err["status"] == "Telemetry degraded"
        assert err["last_successful_update"].startswith("2026-10-23T07:00")
        assert "Traceback" not in r.text and "connection refused" not in r.text


def test_health_reports_prometheus_state():
    assert TestClient(create_app(load_settings(), FakeProm())).get("/api/health").json()["prometheus"] == "connected"
    r = TestClient(create_app(load_settings(), DownProm())).get("/api/health")
    assert r.status_code == 200 and r.json()["prometheus"] == "unreachable"
