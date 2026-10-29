"""Collector v2: Prometheus -> one CSV, joined with diurnal.py's load_log.csv.

Fixes vs v1: no-series req_rate -> 0, warm-up rows dropped, mean latency instead of
the unusable highr p95, ready replicas + CPU request columns, ground-truth join.

Usage:
  python collector_v2.py --start 2026-10-15T20:00:00 --end 2026-10-15T22:00:00 --log load_log.csv --out run1.csv
  (times are UTC; or use --hours N for "the last N hours")
Needs: kubectl -n monitoring port-forward svc/monitoring-kube-prometheus-prometheus 9090
"""
import argparse
import csv
import re
import time
from datetime import datetime, timezone

import requests

PROM = "http://localhost:9090"
STEP = 15
NS = 'namespace="default"'
SERVICES = {"frontend", "backend", "db-service"}

# metric -> (promql, label that identifies the service, aggregation when several series map to one service)
Q = {
    "cpu_m": (f'sum by (pod)(rate(container_cpu_usage_seconds_total{{{NS},container!=""}}[1m]))*1000', "pod", "sum"),
    "mem_mib": (f'sum by (pod)(container_memory_working_set_bytes{{{NS},container!=""}})/1024/1024', "pod", "sum"),
    "req_rate": (f'sum by (job)(rate(http_requests_total{{{NS},handler!="/metrics"}}[1m]))', "job", "sum"),
    "lat_mean_s": (f'sum by (job)(rate(http_request_duration_seconds_sum{{{NS},handler!="/metrics"}}[1m]))'
                   f'/sum by (job)(rate(http_request_duration_seconds_count{{{NS},handler!="/metrics"}}[1m]))', "job", "last"),
    "replicas": (f'sum by (deployment)(kube_deployment_status_replicas_available{{{NS}}})', "deployment", "last"),
    "cpu_req_m": (f'sum by (pod)(kube_pod_container_resource_requests{{{NS},resource="cpu"}})*1000', "pod", "sum"),
}
COLS = ["cpu_m", "mem_mib", "req_rate", "lat_mean_s", "replicas", "cpu_req_m"]


def svc_of(label, value):
    if label == "pod":  # db-service-544cc5b986-s776g -> db-service
        return re.sub(r"-[a-z0-9]+-[a-z0-9]+$", "", value)
    return value


def query_range(q, start, end):
    out, t = [], start
    while t < end:  # chunk to stay under Prometheus' 11k-points-per-series limit
        ce = min(t + 3 * 3600, end)
        r = requests.get(f"{PROM}/api/v1/query_range",
                         params={"query": q, "start": t, "end": ce, "step": STEP}, timeout=60)
        r.raise_for_status()
        out += r.json()["data"]["result"]
        t = ce
    return out


def parse_ts(s):
    d = datetime.fromisoformat(s)
    return int((d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp())


def load_log(path):
    rows = {}
    with open(path) as f:
        for r in csv.DictReader(f):
            rows[int(r["ts"])] = r
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start"); ap.add_argument("--end"); ap.add_argument("--hours", type=float)
    ap.add_argument("--log"); ap.add_argument("--out", default="dataset.csv")
    a = ap.parse_args()
    if a.hours:
        end = int(time.time()); start = end - int(a.hours * 3600)
    else:
        start, end = parse_ts(a.start), parse_ts(a.end)

    data = {}
    for metric, (q, label, agg) in Q.items():
        for s in query_range(q, start, end):
            svc = svc_of(label, s["metric"].get(label, ""))
            if svc not in SERVICES:
                continue
            for ts, val in s["values"]:
                if val in ("NaN", "+Inf", "-Inf"):
                    continue
                row = data.setdefault((int(float(ts)), svc), {})
                v = float(val)
                row[metric] = row.get(metric, 0.0) + v if agg == "sum" else v

    # clean: drop warm-up rows (no cpu yet); no http series = no traffic = 0
    for k in [k for k, r in data.items() if "cpu_m" not in r]:
        del data[k]
    for r in data.values():
        r.setdefault("req_rate", 0.0)

    log = load_log(a.log) if a.log else {}
    extra = ["target_rps", "phase", "spike", "replicas_cmd"]
    with open(a.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp_utc", "service"] + COLS + extra)
        for (ts, svc) in sorted(data):
            r = data[(ts, svc)]
            # frontend receives the generated load; backend sees the same rate; db-service gets none
            g = log.get(ts) or log.get(ts - 1) or log.get(ts + 1) or {}
            tgt = (g.get("target_rps", "") if svc != "db-service" else 0)
            w.writerow([datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds"), svc]
                       + [round(r[c], 4) if c in r else "" for c in COLS]
                       + [tgt, g.get("phase", ""), g.get("spike", ""), g.get("replicas_cmd", "")])
    print(f"{len(data)} rows -> {a.out}")


if __name__ == "__main__":
    main()