# AKOA: Autonomous Kubernetes Operations Agent

AKOA observes a Kubernetes environment, works out what it costs and where resources are wasted, and (in later phases) forecasts load, recommends actions and executes approved changes safely.

> **Core idea:** Observe → Understand → Decide → Optimize. A deterministic policy engine sits between any AI suggestion and Kubernetes. The LLM explains and proposes; it never executes, sends email, or bypasses policy.

This README gets the **part that exists today** running on your machine: a three-service demo app on Minikube, Prometheus + Grafana, a load/dataset toolkit, and a read-only AKOA API with a cost engine.

---

## 1. Project status

| Phase | Status |
|---|---|
| Local Kubernetes (Minikube) + demo microservices | Done |
| Observability (Prometheus, Grafana, kube-state-metrics, app `/metrics`) | Done |
| Workload simulation + dataset collection | Done |
| AKOA read-only API (services, metrics, overview) | Done |
| Cost engine + pricing config | Done |
| Load forecasting | On hold |
| Policy / decision engine (recommendations, risk) | Next |
| Action pipeline + email approval, LLM agent, React UI | Not started (UI designed in Figma) |

## 2. Architecture (current)

```
 load scripts ──► frontend (8001) ──► backend (8000)        db-service (8002, idle on purpose)
                       │                  │                        │
                       └──────── /metrics ┴────────────────────────┘
                                         │
                      Prometheus + kube-state-metrics + cAdvisor (namespace: monitoring)
                                         │
                     ┌───────────────────┴───────────────┐
                     ▼                                   ▼
              AKOA API (8100)                   tools/collector_v2.py
        services · metrics · costs                  (CSV datasets)
                     │
                 Grafana (3000), optional
```

`db-service` is deliberately not called by anything. It is the ground-truth **idle service** for waste detection.

## 3. Prerequisites

| Tool | Notes |
|---|---|
| Docker (Docker Desktop on Windows/macOS) | Must be running |
| [Minikube](https://minikube.sigs.k8s.io/docs/start/) | Docker driver |
| kubectl | |
| [Helm](https://helm.sh/docs/intro/install/) | 3.x or 4.x |
| Python 3.10+ | For the API and tools |
| ~6 GB free RAM for Minikube | The monitoring stack is memory-hungry |

**Windows:** run `minikube`, `kubectl`, `helm` and `python` from the **same PowerShell**, and keep the project on a normal Windows path such as `C:\dev\akoa`, not under `\\wsl.localhost\...`. Mixing WSL and PowerShell breaks kubeconfig (see Troubleshooting).

## 4. Repository layout

```
akoa/
├── k8s/            Deployments, Services, ServiceMonitor
├── services/
│   ├── frontend/   FastAPI, :8001, calls backend at /backend
│   ├── backend/    FastAPI, :8000, /work sleeps 100 ms
│   └── db/         FastAPI, :8002, simulated DB (idle)
├── tools/
│   ├── diurnal.py        Day/night load generator with replica-scaling schedule
│   ├── collector_v2.py   Prometheus → CSV, joined with the load log
│   ├── runner.py         v1 staged scenarios (older)
│   └── collector.py      v1 collector (older)
└── api/            AKOA API (FastAPI)
    ├── app/        routes, Prometheus client, status rules, cost engine
    ├── tests/      56 tests, no cluster needed
    ├── pricing.yaml        Cost model (edit this)
    └── dependencies.yaml   Service dependency map (edit this)
```

Every service exposes `/`, `/health`, `/work` and `/metrics` (via `prometheus-fastapi-instrumentator`). The frontend adds `/backend`.

## 5. Quick start

All commands run from the repository root. Commands are identical in PowerShell and bash unless marked.

### 5.1 Start Minikube

```
minikube start --cpus=4 --memory=6144
kubectl get nodes
```

The node should show `Ready`.

### 5.2 Install Prometheus + Grafana

Install this **before** applying `k8s/`: the ServiceMonitor needs the CRDs this chart creates. The extra flags keep 14 days of metrics on a persistent volume, so history survives a Prometheus restart.

```
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm install monitoring prometheus-community/kube-prometheus-stack -n monitoring --create-namespace --set prometheus.prometheusSpec.retention=14d --set "prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.spec.resources.requests.storage=5Gi" --set "prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.spec.accessModes[0]=ReadWriteOnce"
kubectl get pods -n monitoring
```

Wait 2-5 minutes until every pod is `Running` (Grafana shows `3/3`). If pods stay `Pending`, Minikube is short on memory; restart it with more (`minikube stop`, then `minikube start --cpus=4 --memory=6144`).

### 5.3 Build the images into Minikube

Deployments use `imagePullPolicy: Never`, so images must exist **inside** Minikube. Each tag must match its own folder.

```
minikube image build -t akoa-backend:1.0 ./services/backend
minikube image build -t akoa-frontend:1.0 ./services/frontend
minikube image build -t akoa-db:1.0 ./services/db
```

### 5.4 Deploy

```
kubectl apply -f k8s/
kubectl get pods,svc
```

Expect `backend`, `frontend` and `db-service` pods `Running` and `1/1`. Only `frontend` is a `NodePort`; the others are `ClusterIP`. If you inherited a leftover `nginx` deployment, remove it (`kubectl delete deploy nginx`) so it doesn't pollute the data.

### 5.5 Verify the service chain

Keep this running in its own terminal (the port changes every time):

```
minikube service frontend --url
```

In another terminal, using the printed URL:

```
curl http://127.0.0.1:<port>/
curl http://127.0.0.1:<port>/backend
```

(PowerShell: use `curl.exe`; plain `curl` is an alias for `Invoke-WebRequest`.)

Expected:
```
{"service":"frontend","status":"running"}
{"service":"frontend","backend_response":{"message":"work completed"}}
```

### 5.6 Open Prometheus and confirm scraping

```
kubectl -n monitoring port-forward svc/monitoring-kube-prometheus-prometheus 9090
```

Open <http://localhost:9090/targets>. `serviceMonitor/monitoring/akoa-services/0` should show **3 targets, all UP**. Then try:

```
sum(rate(http_requests_total{handler!="/metrics"}[1m])) by (job)
```

### 5.7 Run the AKOA API

```
cd api
python -m venv .venv
```

Activate it: PowerShell `.\.venv\Scripts\Activate.ps1`, bash `source .venv/bin/activate`. Then:

```
pip install -r requirements-dev.txt
pytest
uvicorn app.main:app --reload --port 8100
```

`pytest` should print `56 passed` (it uses a fake Prometheus). The API needs the Prometheus port-forward from 5.6 running. Open <http://localhost:8100/docs> for interactive docs.

### 5.8 Grafana (optional)

```
kubectl -n monitoring port-forward svc/monitoring-grafana 3000:80
```

Open <http://localhost:3000>, user `admin`. Password:

```
# bash
kubectl -n monitoring get secret monitoring-grafana -o jsonpath="{.data.admin-password}" | base64 -d; echo
# PowerShell
[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String((kubectl -n monitoring get secret monitoring-grafana -o jsonpath="{.data.admin-password}")))
```

### Terminals you will have open

| # | Command | Why |
|---|---|---|
| 1 | `minikube service frontend --url` | Tunnel to the frontend (Docker driver on Windows/macOS) |
| 2 | `kubectl -n monitoring port-forward svc/monitoring-kube-prometheus-prometheus 9090` | API and collector read from this |
| 3 | `uvicorn app.main:app --port 8100` (in `api/`) | AKOA API |
| 4 | Anything else (load generator, curl) | |

## 6. AKOA API

Base URL `http://localhost:8100`. Everything is read-only (GET).

| Endpoint | Returns |
|---|---|
| `/api/health` | API up, and whether Prometheus is reachable |
| `/api/overview` | Summary tiles, service table, telemetry source status (facts only, no recommendations) |
| `/api/services` | One summary per service: status, replicas, CPU/memory vs request, req/s, error rate, latency |
| `/api/services/{name}` | Summary plus pods, configured dependencies, 10-minute analysis-window averages |
| `/api/metrics/{name}?range=15m\|1h\|6h\|24h\|7d` | Time series with min/avg/max/last |
| `/api/costs?usage_window=10m\|1h\|6h\|24h&profile=` | Allocated, used and unused monthly cost per service |
| `/api/costs/estimate?service=&replicas=&profile=` | What-if: monthly cost of a different replica count |
| `/api/costs/profiles` | Configured pricing profiles |

Examples:

```
curl "http://localhost:8100/api/services"
curl "http://localhost:8100/api/costs?usage_window=1h"
curl "http://localhost:8100/api/costs/estimate?service=backend&replicas=3"
```

**How to read the numbers**
- **CPU %** is usage as a percentage of the CPU **request**, so it can exceed 100%.
- **Status** is one of Healthy, Underutilized, Warning, Degraded, Critical, Unknown, and each response includes `status_reasons` explaining why. Thresholds are env vars (below).
- **`req_rate`**: `0` means the app is scraped and idle; `null` means it isn't scraped, so AKOA can't call it idle.
- **Costs are modeled.** Minikube has no real bill. Allocated cost is what the requested CPU/memory would cost on the selected pricing profile; used cost applies the same rates to average actual usage; the gap is the waste. Every cost response carries an `assumptions` block (rates, exchange rate, multiplier, source). The estimate endpoint is arithmetic only and says nothing about whether a change is safe.
- **Errors** share one shape: `{"error": {"code", "message"}}`. If Prometheus is unreachable you get `503 telemetry_unavailable` with the last successful update time.

### Configuration

Environment variables (all optional):

| Variable | Default |
|---|---|
| `AKOA_PROMETHEUS_URL` | `http://localhost:9090` |
| `AKOA_NAMESPACE` | `default` |
| `AKOA_SERVICES` | `frontend,backend,db-service` (allowlist) |
| `AKOA_ENVIRONMENT` | `Development / Minikube` |
| `AKOA_CORS_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` |
| `AKOA_PRICING_FILE` / `AKOA_DEPENDENCIES_FILE` | `api/pricing.yaml` / `api/dependencies.yaml` |
| `AKOA_WARN_CPU_PCT` | `100` (Warning above this % of request) |
| `AKOA_IDLE_CPU_PCT` / `AKOA_IDLE_REQ_RATE` | `10` / `1` (Underutilized below both) |
| `AKOA_WARN_ERROR_RATE` / `AKOA_DEGRADED_ERROR_RATE` | `0.01` / `0.05` |

**`api/pricing.yaml`** holds the cost model: currency (INR/USD), exchange rate, hours per month, `simulation_multiplier`, and provider profiles. The shipped AWS Fargate (US East) rates were taken from AWS's published per-second prices on 2026-10-23, and the ₹/USD rate is a reference value from 2026-10-22; update both periodically. Minikube pods request very little (100m CPU / 128Mi), so absolute costs are small; set `simulation_multiplier: 10` to model production-sized pods, and say so in any write-up. To add a provider, copy a profile and fill in rates from that provider's own price list. A bad pricing file stops the API at startup with a clear message.

**`api/dependencies.yaml`** declares which service calls which. It is configured knowledge, not observed traffic, and the API labels it that way.

## 7. Generating load and datasets

Needs the frontend tunnel (terminal 1) and the Prometheus port-forward (terminal 2). Install the one dependency: `pip install requests`.

**Generate a compressed "day/night" workload** with random spikes and a replica-scaling schedule, writing a per-second ground-truth log:

```
cd tools
python diurnal.py http://127.0.0.1:<tunnel-port> --period 1800 --days 4 --seed 1 --scale "0.3:2,0.6:3,0.8:1"
```

- `--period` is seconds per simulated day; total runtime is `period × days`. For a quick pipeline test use `--period 300 --days 2` (10 minutes).
- `--scale` is `fraction_of_run:replicas` for the backend. The script restores the original replica count when it ends or you press Ctrl+C.
- `--peak` (default 80) is the daytime req/s. The demo stack saturates around 100-110 req/s.
- **Don't restart any deployment during a run.** The tunnel port changes and the load silently stops.

**Collect the metrics for that run** (times in UTC, from the start and end of the run):

```
python collector_v2.py --start 2026-10-15T20:00:00 --end 2026-10-15T22:00:00 --log load_log.csv --out run1.csv
```

Output: one row per 15 s per service with `cpu_m, mem_mib, req_rate, lat_mean_s, replicas, cpu_req_m` plus the ground truth (`target_rps, phase, spike, replicas_cmd`). Use several runs with different seeds and peaks, and hold one run out for testing any model. `runner.py` and `collector.py` are the older single-pass versions.

## 8. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Pod `ErrImageNeverPull` | Image not built inside Minikube | Run the matching `minikube image build` (5.3), then `kubectl delete pod -l app=<name>` |
| `/backend` returns `{"detail":"Not Found"}` | Running image predates the route | Rebuild the frontend image, `kubectl rollout restart deploy/frontend` |
| `kubectl`/`helm`: `localhost:8080 connection refused` | Shell has no kubeconfig (typical: WSL vs Windows) | Use the same shell where Minikube was started |
| `helm`: not recognized (Windows) | PATH not refreshed after install | Open a new PowerShell window |
| `minikube service` prints a port, then it dies | Tunnel closed | Keep that terminal open; the port changes each time |
| Prometheus target DOWN, `404` on `/metrics` | Pod runs an image without the instrumentator | Check `main.py` and `requirements.txt`, rebuild, restart the deployment |
| Target DOWN, `connection refused` | Service `targetPort` doesn't match the container port | db-service listens on 8002; check the Service manifest |
| No `akoa-services` targets | ServiceMonitor missing label `release: monitoring`, Service missing `monitoring: akoa`, or port not named `http` | Fix the manifests and re-apply |
| API returns `503 telemetry_unavailable` | Prometheus port-forward not running | Start terminal 2 |
| Metrics empty after a Prometheus restart | History was not on a persistent volume | Install with the flags in 5.2; check `kubectl get pvc -n monitoring` |
| `curl` output looks wrong (PowerShell) | `curl` is an alias | Use `curl.exe` |
| Short burst of errors when scaling up | New pods receive traffic before the app is listening | Add a readiness probe (below) |

## 9. Known gaps and recommended fixes

- **Add readiness probes** to all three Deployments. Without them, scaling up briefly sends traffic to pods that aren't listening yet (observed as a few seconds of connection errors during replica changes). Example for backend (use ports 8001 for frontend and 8002 for db-service):
  ```yaml
  readinessProbe:
    httpGet: { path: /health, port: 8000 }
    initialDelaySeconds: 2
    periodSeconds: 2
  ```
- Costs and statuses are computed from CPU/memory **requests**. Services without requests report `available: false` rather than a made-up number.
- Only the AWS Fargate pricing profile is verified so far.
- Kubernetes events, forecasts, recommendations and any change to the cluster are not implemented yet.

## 10. Cleanup

```
kubectl delete -f k8s/
helm uninstall monitoring -n monitoring
minikube delete        # removes the whole cluster and its data
```

## 11. Design principles

- **No fake intelligence.** Anything shown to a user is either measured, configured, or clearly labeled as modeled or demo data.
- **Deterministic policy gates every action.** The LLM can propose; policy and human approval decide.
- **Explainable outputs.** Statuses, costs and (later) recommendations carry their reasons and assumptions.
- **Bottom-up build order.** Measure first, then model, then decide, then act.
