#!/usr/bin/env python3
# ruff: noqa
# (kit-managed file: host-repo lint rules do not apply)
"""Per-class circuit breaker for tier-0 auto-merge. Stateless: derived from GitHub history each time.

A class is demoted (treated as tier 1) when a PR auto-merged under it within the lookback window was later reverted,
or when the default branch's required CI went red on a commit it introduced and was not green again by itself.
Demotion lifts only when the owner removes and re-adds the class in merge-policy.yaml (a PR they merge), which moves
`policy_changed_at` past the offending merge.

  merged: [{"class": str, "merged_at": iso, "sha": str, "reverted": bool, "main_red": bool}]
"""

from __future__ import annotations

import datetime as dt

LOOKBACK_DAYS = 30


def _ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def demoted_classes(
    merged: list[dict], *, now: dt.datetime | None = None, policy_changed_at: str | None = None
) -> dict[str, str]:
    """Return {class: reason} for classes that must fall back to tier 1."""
    now = now or dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=LOOKBACK_DAYS)
    cleared = _ts(policy_changed_at) if policy_changed_at else None
    out: dict[str, str] = {}
    for m in merged:
        at = _ts(m["merged_at"])
        if at < since or (cleared and at <= cleared):
            continue
        if m.get("reverted"):
            out.setdefault(m["class"], f"{m.get('sha', '?')[:7]} was reverted")
        elif m.get("main_red"):
            out.setdefault(m["class"], f"main went red after {m.get('sha', '?')[:7]}")
    return out


def effective_classes(enabled: list[str], demoted: dict[str, str]) -> list[str]:
    return [c for c in enabled if c not in demoted]
