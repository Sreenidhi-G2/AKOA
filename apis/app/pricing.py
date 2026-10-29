"""Pricing model loader. Pure data + arithmetic, no I/O beyond reading the YAML."""
from dataclasses import dataclass
from pathlib import Path

import yaml


class PricingConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Profile:
    name: str
    provider: str
    description: str
    vcpu_hour_usd: float
    gib_hour_usd: float
    source: str
    verified_on: str
    notes: tuple[str, ...]


@dataclass(frozen=True)
class Pricing:
    currency: str
    usd_to_inr: float
    fx_as_of: str
    fx_source: str
    hours_per_month: float
    simulation_multiplier: float
    active_profile: str
    profiles: dict[str, Profile]

    @property
    def fx(self) -> float:
        return self.usd_to_inr if self.currency == "INR" else 1.0

    def profile(self, name: str | None = None) -> Profile:
        return self.profiles[name or self.active_profile]  # KeyError if unknown

    def hourly(self, p: Profile, cpu_cores: float, mem_gib: float) -> float:
        usd = (cpu_cores * p.vcpu_hour_usd + mem_gib * p.gib_hour_usd) * self.simulation_multiplier
        return usd * self.fx

    def monthly(self, p: Profile, cpu_cores: float, mem_gib: float) -> float:
        return self.hourly(p, cpu_cores, mem_gib) * self.hours_per_month


def _positive(d: dict, key: str, where: str) -> float:
    try:
        v = float(d[key])
    except (KeyError, TypeError, ValueError):
        raise PricingConfigError(f"{where}: '{key}' is missing or not a number") from None
    if v <= 0:
        raise PricingConfigError(f"{where}: '{key}' must be greater than 0 (got {v})")
    return v


def parse(raw: dict) -> Pricing:
    if not isinstance(raw, dict):
        raise PricingConfigError("pricing file is empty or not a mapping")
    currency = str(raw.get("currency", "")).upper()
    if currency not in ("INR", "USD"):
        raise PricingConfigError(f"currency must be INR or USD (got '{raw.get('currency')}')")

    fx = raw.get("fx") or {}
    usd_to_inr = _positive(fx, "usd_to_inr", "fx") if currency == "INR" else float(fx.get("usd_to_inr", 1) or 1)

    profiles: dict[str, Profile] = {}
    for name, p in (raw.get("profiles") or {}).items():
        where = f"profile '{name}'"
        profiles[name] = Profile(
            name=name,
            provider=str(p.get("provider", "")),
            description=str(p.get("description", "")),
            vcpu_hour_usd=_positive(p, "vcpu_hour_usd", where),
            gib_hour_usd=_positive(p, "gib_hour_usd", where),
            source=str(p.get("source", "")),
            verified_on=str(p.get("verified_on", "")),
            notes=tuple(str(n) for n in (p.get("notes") or [])),
        )
    if not profiles:
        raise PricingConfigError("no pricing profiles defined")

    active = raw.get("active_profile")
    if active not in profiles:
        raise PricingConfigError(f"active_profile '{active}' is not one of {sorted(profiles)}")

    return Pricing(
        currency=currency,
        usd_to_inr=usd_to_inr,
        fx_as_of=str(fx.get("as_of", "")),
        fx_source=str(fx.get("source", "")),
        hours_per_month=_positive(raw, "hours_per_month", "pricing"),
        simulation_multiplier=_positive(raw, "simulation_multiplier", "pricing"),
        active_profile=active,
        profiles=profiles,
    )


def load(path: Path) -> Pricing:
    try:
        raw = yaml.safe_load(Path(path).read_text())
    except FileNotFoundError:
        raise PricingConfigError(f"pricing file not found: {path}") from None
    except yaml.YAMLError as e:
        raise PricingConfigError(f"pricing file is not valid YAML: {e}") from None
    return parse(raw)
