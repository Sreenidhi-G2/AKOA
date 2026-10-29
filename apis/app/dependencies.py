"""Service dependency map, loaded from dependencies.yaml.

This is CONFIGURED knowledge, not observed traffic, and the API labels it that way.
(Later it can be validated against real call metrics or a service mesh.)
"""
import yaml

from .config import Settings


def load(settings: Settings) -> dict[str, dict[str, list[str]]]:
    try:
        raw = yaml.safe_load(settings.dependencies_file.read_text()) or {}
    except FileNotFoundError:
        raw = {}
    declared = raw.get("services", {}) or {}
    upstream = {svc: list((declared.get(svc) or {}).get("depends_on", []) or []) for svc in settings.services}
    downstream: dict[str, list[str]] = {svc: [] for svc in settings.services}
    for svc, deps in upstream.items():
        for dep in deps:
            if dep in downstream:
                downstream[dep].append(svc)
    return {svc: {"upstream": upstream[svc], "downstream": sorted(downstream[svc])} for svc in settings.services}
