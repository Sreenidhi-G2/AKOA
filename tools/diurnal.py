"""Diurnal load generator for AKOA datasets.

Simulates compressed "days": a smooth day/night traffic cycle + noise + random
spikes, sent open-loop at a target request rate. Optionally scales a deployment
on a schedule so the dataset contains replica changes. Writes a per-second
ground-truth log (load_log.csv) that collector_v2.py joins onto the metrics.

Usage (PowerShell, tunnel + Prometheus port-forward already running):
  python diurnal.py http://127.0.0.1:<tunnel-port> --period 1800 --days 4 --seed 1 --scale "0.3:2,0.6:3,0.85:1"
"""
import argparse
import csv
import math
import queue
import random
import subprocess
import threading
import time

import requests

ap = argparse.ArgumentParser()
ap.add_argument("url")
ap.add_argument("--period", type=int, default=1800, help="seconds per simulated day")
ap.add_argument("--days", type=int, default=4)
ap.add_argument("--peak", type=float, default=80, help="daytime peak req/s")
ap.add_argument("--night", type=float, default=3, help="night floor req/s")
ap.add_argument("--cap", type=float, default=100, help="hard ceiling req/s (stack saturates ~100-110)")
ap.add_argument("--noise", type=float, default=0.10, help="relative gaussian noise")
ap.add_argument("--spike-prob", type=float, default=0.0015, help="chance per second a spike starts")
ap.add_argument("--spike-mult", type=float, default=2.5)
ap.add_argument("--seed", type=int, default=1)
ap.add_argument("--workers", type=int, default=40)
ap.add_argument("--scale", default="", help='e.g. "0.3:2,0.6:3,0.85:1" = fraction_of_run:replicas')
ap.add_argument("--deploy", default="backend", help="deployment to scale")
ap.add_argument("--out", default="load_log.csv")
a = ap.parse_args()

rng = random.Random(a.seed)
total = a.period * a.days
url = a.url.rstrip("/") + "/backend"

# fail fast if the tunnel is down
requests.get(a.url.rstrip("/") + "/", timeout=5).raise_for_status()

schedule = {}
for part in filter(None, a.scale.split(",")):
    frac, reps = part.split(":")
    schedule[int(float(frac) * total)] = int(reps)

original = None
if schedule:
    original = subprocess.check_output(
        ["kubectl", "get", "deploy", a.deploy, "-o", "jsonpath={.spec.replicas}"], text=True).strip()


def base_rate(t):
    phase = (t % a.period) / a.period  # starts at night, peaks mid-"day"
    return a.night + (a.peak - a.night) * 0.5 * (1 - math.cos(2 * math.pi * phase))


def phase_label(rate):
    return "low" if rate < 0.2 * a.peak else ("high" if rate > 0.6 * a.peak else "mid")


tokens = queue.Queue(maxsize=300)
stop = threading.Event()
lock = threading.Lock()
stats = {"sent": 0, "errors": 0, "dropped": 0}


def worker():
    s = requests.Session()
    while not stop.is_set():
        try:
            tokens.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            r = s.get(url, timeout=5)
            ok = r.status_code < 500
        except requests.RequestException:
            ok = False
        with lock:
            stats["sent" if ok else "errors"] += 1


threads = [threading.Thread(target=worker, daemon=True) for _ in range(a.workers)]
for t in threads:
    t.start()

start = time.time()
carry = 0.0
spike_until = -1
replicas_cmd = ""
last = dict(stats)

try:
    with open(a.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "target_rps", "sent", "errors", "dropped", "phase", "spike", "replicas_cmd"])
        for i in range(total):
            time.sleep(max(0, start + i - time.time()))

            if i in schedule:
                replicas_cmd = schedule[i]
                subprocess.Popen(["kubectl", "scale", f"deploy/{a.deploy}", f"--replicas={replicas_cmd}"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            if i >= spike_until and rng.random() < a.spike_prob:
                spike_until = i + rng.randint(30, 60)
            in_spike = i < spike_until

            rate = base_rate(i) * (a.spike_mult if in_spike else 1.0)
            rate = min(a.cap, max(0.0, rate * rng.gauss(1.0, a.noise)))

            carry += rate
            n = int(carry)
            carry -= n
            for _ in range(n):
                try:
                    tokens.put_nowait(1)
                except queue.Full:
                    with lock:
                        stats["dropped"] += 1

            with lock:
                snap = dict(stats)
            w.writerow([int(start + i), round(rate, 2), snap["sent"] - last["sent"],
                        snap["errors"] - last["errors"], snap["dropped"] - last["dropped"],
                        phase_label(rate), int(in_spike), replicas_cmd])
            last = snap
            if i % 10 == 0:
                f.flush()
            if i % 60 == 0:
                print(f"t={i}/{total}s  target={rate:5.1f} req/s  spike={in_spike}  replicas_cmd={replicas_cmd}")
finally:
    stop.set()
    if original is not None:
        subprocess.run(["kubectl", "scale", f"deploy/{a.deploy}", f"--replicas={original}"])
        print(f"restored {a.deploy} to {original} replicas")

print(f"done -> {a.out}")