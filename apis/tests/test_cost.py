import pytest
from fastapi.testclient import TestClient

from app import pricing as pricing_mod
from app.config import load_settings
from app.main import create_app
from app.pricing import PricingConfigError
from tests.fakes import DownProm, FakeProm

SETTINGS = load_settings()


@pytest.fixture
def client():
    return TestClient(create_app(SETTINGS, FakeProm()))


def by_name(rows):
    return {r["name"]: r for r in rows}


# ---------------------------------------------------------------- pricing model
def test_default_pricing_file_loads_and_matches_published_rates():
    pr = pricing_mod.load(SETTINGS.pricing_file)
    p = pr.profile()
    # AWS publishes per-second rates: $0.000011244 / vCPU-s and $0.000001235 / GB-s
    assert p.vcpu_hour_usd == pytest.approx(0.000011244 * 3600, rel=1e-4)
    assert p.gib_hour_usd == pytest.approx(0.000001235 * 3600, rel=1e-3)
    assert pr.currency == "INR" and pr.simulation_multiplier == 1.0


def test_monthly_formula_by_hand():
    pr = pricing_mod.load(SETTINGS.pricing_file)
    p = pr.profile()
    usd = (0.1 * p.vcpu_hour_usd + 0.125 * p.gib_hour_usd) * 730
    assert pr.monthly(p, 0.1, 0.125) == pytest.approx(usd * pr.usd_to_inr)


@pytest.mark.parametrize(
    "mutate, msg",
    [
        (lambda r: r.update(currency="EUR"), "currency"),
        (lambda r: r["profiles"]["aws-fargate-us-east-1"].update(vcpu_hour_usd=-1), "vcpu_hour_usd"),
        (lambda r: r["profiles"]["aws-fargate-us-east-1"].pop("gib_hour_usd"), "gib_hour_usd"),
        (lambda r: r.update(active_profile="nope"), "active_profile"),
        (lambda r: r.update(simulation_multiplier=0), "simulation_multiplier"),
        (lambda r: r.update(profiles={}), "no pricing profiles"),
    ],
)
def test_bad_pricing_config_is_rejected(mutate, msg):
    import yaml

    raw = yaml.safe_load(SETTINGS.pricing_file.read_text())
    mutate(raw)
    with pytest.raises(PricingConfigError, match=msg):
        pricing_mod.parse(raw)


def test_missing_pricing_file_fails_fast(tmp_path):
    with pytest.raises(PricingConfigError, match="not found"):
        pricing_mod.load(tmp_path / "nope.yaml")


# ---------------------------------------------------------------- /api/costs
def test_costs_numbers(client):
    body = client.get("/api/costs").json()
    rows = by_name(body["services"])
    assert set(rows) == {"frontend", "backend", "db-service"}  # nginx excluded
    be = rows["backend"]
    # 2 replicas x (100m CPU + 128 MiB) at the Fargate rates, converted to INR (worked out by hand)
    assert be["cpu_request_m_per_replica"] == 100 and be["mem_request_mib_per_replica"] == 128
    assert be["cost_per_replica_monthly"] == pytest.approx(318.6, abs=0.5)
    assert be["allocated_monthly"] == pytest.approx(637.2, abs=1.0)
    # used: 90m CPU + 100 MiB average (canned) at the same unit rates
    assert be["used_monthly"] == pytest.approx(282.2, abs=1.0)
    assert be["unused_monthly"] == pytest.approx(be["allocated_monthly"] - be["used_monthly"], abs=0.02)
    assert be["unused_pct"] == pytest.approx(be["unused_monthly"] / be["allocated_monthly"] * 100, abs=0.1)


def test_totals_are_sums(client):
    body = client.get("/api/costs").json()
    t = body["totals"]
    assert t["allocated_monthly"] == pytest.approx(sum(r["allocated_monthly"] for r in body["services"]), abs=0.05)
    assert t["unused_monthly"] == pytest.approx(sum(r["unused_monthly"] for r in body["services"]), abs=0.05)


def test_assumptions_are_always_attached(client):
    a = client.get("/api/costs").json()["assumptions"]
    assert a["currency"] == "INR" and a["billing_basis"] == "requested"
    assert a["fx"]["usd_to_inr"] > 0 and a["fx"]["as_of"]
    assert a["price_source"].startswith("https://aws.amazon.com/fargate")
    assert a["simulation_multiplier"] == 1.0 and a["hours_per_month"] == 730
    assert "Minikube has no real cloud bill" in a["disclaimer"]
    assert a["notes"]  # known simplifications are surfaced, not buried


def test_usage_window_is_validated_and_sent_to_prometheus():
    prom = FakeProm()
    c = TestClient(create_app(SETTINGS, prom))
    assert c.get("/api/costs?usage_window=6h").json()["usage_window"] == "6h"
    assert any("[6h:1m]" in q for q in prom.queries)
    assert c.get("/api/costs?usage_window=1y").status_code == 422
    assert c.get("/api/costs?usage_window=1h%5D%7Bx").status_code == 422  # no PromQL injection via the window


def test_costs_unavailable_when_requests_missing():
    rules = [r for r in __import__("tests.fakes", fromlist=["x"]).FLEET_RULES if "resource_requests" not in r[0]]
    c = TestClient(create_app(SETTINGS, FakeProm(rules=rules)))
    body = c.get("/api/costs").json()
    assert all(r["available"] is False and r["allocated_monthly"] is None for r in body["services"])
    assert body["totals"]["allocated_monthly"] == 0 and body["totals"]["unused_pct"] is None


# ---------------------------------------------------------------- what-if estimate
def test_estimate_scale_down_saves_one_replica(client):
    e = client.get("/api/costs/estimate?service=backend&replicas=1").json()
    assert e["replicas_current"] == 2 and e["replicas_new"] == 1
    assert e["monthly_delta"] == pytest.approx(-e["cost_per_replica_monthly"], abs=0.02)
    assert e["monthly_new"] == pytest.approx(e["monthly_current"] / 2, abs=0.02)
    assert e["pct_change"] == pytest.approx(-50.0, abs=0.1)
    assert e["assumptions"]["currency"] == "INR" and e["notes"]


def test_estimate_scale_up_costs_more(client):
    e = client.get("/api/costs/estimate?service=frontend&replicas=3").json()
    assert e["monthly_delta"] > 0 and e["pct_change"] == pytest.approx(200.0, abs=0.1)


def test_estimate_to_zero_removes_all_cost(client):
    e = client.get("/api/costs/estimate?service=db-service&replicas=0").json()
    assert e["monthly_new"] == 0 and e["monthly_delta"] == pytest.approx(-e["monthly_current"], abs=0.02)


@pytest.mark.parametrize("replicas", ["-1", "101", "abc"])
def test_estimate_replicas_validated(client, replicas):
    assert client.get(f"/api/costs/estimate?service=backend&replicas={replicas}").status_code == 422


def test_estimate_unknown_service_never_reaches_prometheus():
    prom = FakeProm()
    r = TestClient(create_app(SETTINGS, prom)).get("/api/costs/estimate?service=nginx&replicas=1")
    assert r.status_code == 404 and r.json()["error"]["code"] == "service_not_found" and prom.queries == []


def test_estimate_without_requests_is_409():
    rules = [r for r in __import__("tests.fakes", fromlist=["x"]).FLEET_RULES if "resource_requests" not in r[0]]
    r = TestClient(create_app(SETTINGS, FakeProm(rules=rules))).get("/api/costs/estimate?service=backend&replicas=1")
    assert r.status_code == 409 and r.json()["error"]["code"] == "cost_unavailable"


# ---------------------------------------------------------------- profiles / multiplier / errors
def _two_profile_pricing(multiplier=1.0):
    import yaml

    raw = yaml.safe_load(SETTINGS.pricing_file.read_text())
    raw["simulation_multiplier"] = multiplier
    raw["profiles"]["cheap"] = {**raw["profiles"]["aws-fargate-us-east-1"], "provider": "test",
                                "vcpu_hour_usd": 0.01, "gib_hour_usd": 0.001}
    return pricing_mod.parse(raw)


def test_profiles_listing_and_override():
    c = TestClient(create_app(SETTINGS, FakeProm(), _two_profile_pricing()))
    listing = c.get("/api/costs/profiles").json()
    assert listing["active"] == "aws-fargate-us-east-1"
    assert {p["name"] for p in listing["profiles"]} == {"aws-fargate-us-east-1", "cheap"}
    default = c.get("/api/costs").json()["totals"]["allocated_monthly"]
    cheap = c.get("/api/costs?profile=cheap").json()
    assert cheap["assumptions"]["profile"] == "cheap" and cheap["totals"]["allocated_monthly"] < default
    r = c.get("/api/costs?profile=nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "profile_not_found"


def test_simulation_multiplier_scales_costs_and_is_disclosed():
    base = TestClient(create_app(SETTINGS, FakeProm(), _two_profile_pricing(1.0))).get("/api/costs").json()
    big = TestClient(create_app(SETTINGS, FakeProm(), _two_profile_pricing(10.0))).get("/api/costs").json()
    assert big["assumptions"]["simulation_multiplier"] == 10.0
    assert big["totals"]["allocated_monthly"] == pytest.approx(base["totals"]["allocated_monthly"] * 10, rel=1e-3)


def test_costs_when_prometheus_down_is_503():
    c = TestClient(create_app(SETTINGS, DownProm()))
    for path in ("/api/costs", "/api/costs/estimate?service=backend&replicas=1"):
        r = c.get(path)
        assert r.status_code == 503 and r.json()["error"]["code"] == "telemetry_unavailable"
    assert c.get("/api/costs/profiles").status_code == 200  # static config needs no Prometheus
