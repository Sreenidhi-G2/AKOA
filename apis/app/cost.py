"""Cost engine: allocated vs used vs unused, plus a what-if replica estimate.

Deterministic arithmetic over configured unit prices (pricing.yaml). No AI, no
guessing: every output can be reproduced by hand from the `assumptions` block.
"""
import asyncio
from dataclasses import dataclass

from . import queries
from .config import Settings
from .errors import ApiError
from .pricing import Pricing, Profile
from .prometheus import PrometheusClient, scalar
from .schemas import (
    CostAssumptions,
    CostEstimate,
    CostsResponse,
    CostTotals,
    FxInfo,
    ProfileInfo,
    ProfilesResponse,
    ServiceCost,
    ServiceSummary,
)
from .telemetry import Fleet, now_iso

DISCLAIMER = (
    "Modeled estimate from configured unit prices. Minikube has no real cloud bill; "
    "figures show what these resource requests would cost on the selected profile."
)


def _r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(x, nd)


def assumptions(pricing: Pricing, p: Profile) -> CostAssumptions:
    return CostAssumptions(
        profile=p.name,
        provider=p.provider,
        description=p.description,
        currency=pricing.currency,  # type: ignore[arg-type]
        vcpu_hour=round(p.vcpu_hour_usd * pricing.fx, 6),
        gib_hour=round(p.gib_hour_usd * pricing.fx, 6),
        hours_per_month=pricing.hours_per_month,
        simulation_multiplier=pricing.simulation_multiplier,
        fx=FxInfo(usd_to_inr=pricing.usd_to_inr, as_of=pricing.fx_as_of, source=pricing.fx_source)
        if pricing.currency == "INR"
        else None,
        price_source=p.source,
        price_verified_on=p.verified_on,
        notes=list(p.notes),
        disclaimer=DISCLAIMER,
    )


def profiles(pricing: Pricing) -> ProfilesResponse:
    return ProfilesResponse(
        currency=pricing.currency,  # type: ignore[arg-type]
        active=pricing.active_profile,
        profiles=[
            ProfileInfo(
                name=p.name,
                provider=p.provider,
                description=p.description,
                vcpu_hour=round(p.vcpu_hour_usd * pricing.fx, 6),
                gib_hour=round(p.gib_hour_usd * pricing.fx, 6),
                verified_on=p.verified_on,
                source=p.source,
                active=p.name == pricing.active_profile,
            )
            for p in pricing.profiles.values()
        ],
    )


@dataclass
class Usage:
    cpu_m: float | None
    mem_mib: float | None


async def collect_usage(prom: PrometheusClient, s: Settings, window: str) -> dict[str, Usage]:
    async def one(svc: str) -> tuple[str, Usage]:
        qs = queries.usage_avg(s, svc, window)
        cpu, mem = await asyncio.gather(prom.query(qs["cpu_m"]), prom.query(qs["mem_mib"]))
        return svc, Usage(scalar(cpu), scalar(mem))

    return dict(await asyncio.gather(*(one(svc) for svc in s.services)))


def per_replica(summary: ServiceSummary, n_pods: int) -> tuple[float, float] | None:
    """(cpu_cores, mem_gib) requested by ONE replica, or None if requests are unknown.

    Uses the observed pod count (not desired replicas) as the divisor because the
    summed requests cover the pods that currently exist, including rollout overlap.
    """
    if not summary.cpu_request_m or not summary.mem_request_mib:
        return None
    n = max(1, n_pods)
    return summary.cpu_request_m / n / 1000, summary.mem_request_mib / n / 1024


def service_cost(
    summary: ServiceSummary, n_pods: int, usage: Usage | None, pricing: Pricing, p: Profile
) -> ServiceCost:
    unavailable = ServiceCost(
        name=summary.name, available=False, replicas=summary.replicas_desired,
        cpu_request_m_per_replica=None, mem_request_mib_per_replica=None, cost_per_replica_monthly=None,
        allocated_monthly=None, used_monthly=None, unused_monthly=None, unused_pct=None,
        avg_cpu_m=usage.cpu_m if usage else None, avg_mem_mib=usage.mem_mib if usage else None,
    )
    pr = per_replica(summary, n_pods)
    if pr is None or summary.replicas_desired is None:
        return unavailable

    cpu_cores, mem_gib = pr
    replica_cost = pricing.monthly(p, cpu_cores, mem_gib)
    allocated = replica_cost * summary.replicas_desired

    used = unused = unused_pct = None
    if usage and usage.cpu_m is not None and usage.mem_mib is not None:
        used = pricing.monthly(p, usage.cpu_m / 1000, usage.mem_mib / 1024)
        unused = max(0.0, allocated - used)
        unused_pct = unused / allocated * 100 if allocated else None

    return ServiceCost(
        name=summary.name,
        available=True,
        replicas=summary.replicas_desired,
        cpu_request_m_per_replica=_r(cpu_cores * 1000),
        mem_request_mib_per_replica=_r(mem_gib * 1024),
        cost_per_replica_monthly=_r(replica_cost),
        allocated_monthly=_r(allocated),
        used_monthly=_r(used),
        unused_monthly=_r(unused),
        unused_pct=_r(unused_pct, 1),
        avg_cpu_m=_r(usage.cpu_m if usage else None),
        avg_mem_mib=_r(usage.mem_mib if usage else None),
    )


def build_costs(fleet: Fleet, usage: dict[str, Usage], pricing: Pricing, p: Profile, window: str) -> CostsResponse:
    rows = [
        service_cost(summary, len(fleet.pods.get(name, [])), usage.get(name), pricing, p)
        for name, summary in fleet.services.items()
    ]
    alloc = sum(r.allocated_monthly or 0 for r in rows)
    used = sum(r.used_monthly or 0 for r in rows)
    unused = sum(r.unused_monthly or 0 for r in rows)
    return CostsResponse(
        updated_at=now_iso(),
        usage_window=window,  # type: ignore[arg-type]
        assumptions=assumptions(pricing, p),
        services=rows,
        totals=CostTotals(
            allocated_monthly=round(alloc, 2),
            used_monthly=round(used, 2),
            unused_monthly=round(unused, 2),
            unused_pct=round(unused / alloc * 100, 1) if alloc else None,
        ),
    )


def estimate_replicas(
    fleet: Fleet, service: str, new_replicas: int, pricing: Pricing, p: Profile
) -> CostEstimate:
    summary = fleet.services[service]
    pr = per_replica(summary, len(fleet.pods.get(service, [])))
    if pr is None or summary.replicas_desired is None:
        raise ApiError(
            409,
            "cost_unavailable",
            f"Cannot estimate cost for '{service}': CPU/memory requests or replica count are not available.",
        )
    replica_cost = pricing.monthly(p, *pr)
    current = replica_cost * summary.replicas_desired
    new = replica_cost * new_replicas
    return CostEstimate(
        service=service,
        replicas_current=summary.replicas_desired,
        replicas_new=new_replicas,
        cost_per_replica_monthly=round(replica_cost, 2),
        monthly_current=round(current, 2),
        monthly_new=round(new, 2),
        monthly_delta=round(new - current, 2),
        pct_change=round((new - current) / current * 100, 1) if current else None,
        assumptions=assumptions(pricing, p),
        notes=[
            "Arithmetic on the service's current per-replica requests; it does not predict traffic or performance.",
            "Assumes cost scales linearly with replica count.",
            "Whether the change is safe is decided by the policy engine, not by this estimate.",
        ],
    )
