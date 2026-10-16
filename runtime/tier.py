#!/usr/bin/env python3
# ruff: noqa
# (kit-managed file: host-repo lint rules do not apply)
"""Autopilot PR risk tier. Deterministic (no LLM), zero tokens, run from the base branch copy.

  tier 2  touches a protected path (workflows, .autopilot, .claude, migrations, billing): CODEOWNERS blocks auto-merge
  tier 0  every file fits ONE change class AND that class is listed in merge-policy `auto_merge_classes`
  tier 1  everything else: lands in the owner's inbox

`auto_merge_classes` ships empty, so tier 0 never happens until the owner opts a class in (a loosening: it lives in
git under CODEOWNERS). `eligible_class` is reported even when the class is not enabled, so the UI can suggest it.

Classes: docs-only, tests-add-only, lockfile-patch (package-lock.json), generated (owner-listed globs).
(formatter-only needs a tool run, not a diff read, so it is not offered here.)

  tier.py --base origin/main [--head HEAD] [--policy .autopilot/merge-policy.yaml]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from guard import DEFAULT_PROTECTED, FileChange, _TEST_PATH_RE, _matches, _git, parse_diff  # noqa: E402

PROTECTED_TIER2 = DEFAULT_PROTECTED + [
    "supabase/**",
    "**/migrations/**",
    "migrations/**",
    "**/billing/**",
    "**/stripe/**",
    "vercel.json",
    "Dockerfile",
    ".env*",
]
DOC_GLOBS = ["*.md", "docs/**", "LICENSE", "LICENSE.*"]
CLASSES = ["docs-only", "tests-add-only", "lockfile-patch", "generated"]
_WEAKEN_RE = re.compile(r"\.skip\b|\bxfail\b|\bskipif\b|\.only\b|@pytest\.mark\.skip|\bxit\(|\bxdescribe\(|\btodo\b", re.I)
_VER_RE = re.compile(r'^\s*"version":\s*"(\d+)\.(\d+)\.(\d+)"[,]?\s*$')
_LOCK_NOISE_RE = re.compile(r'^\s*"(resolved|integrity)":')


def _is_doc(p: str) -> bool:
    return _matches(p, DOC_GLOBS) and not _matches(p, PROTECTED_TIER2)


def _is_test_only(ch: FileChange) -> bool:
    if not _TEST_PATH_RE.search(ch.path):
        return False
    if ch.status == "A":
        pass
    elif ch.status == "M" and not ch.removed:
        pass
    else:
        return False
    return not any(_WEAKEN_RE.search(ln) for ln in ch.added)


def _is_lock_patch(ch: FileChange) -> bool:
    if os.path.basename(ch.path) != "package-lock.json" or ch.status != "M":
        return False
    old = [m.groups() for ln in ch.removed if (m := _VER_RE.match(ln))]
    new = [m.groups() for ln in ch.added if (m := _VER_RE.match(ln))]
    if not old or len(old) != len(new):
        return False
    for ln in ch.added + ch.removed:
        if not _VER_RE.match(ln) and not _LOCK_NOISE_RE.match(ln):
            return False
    # every bump must be patch-only and upward, pairwise in order
    return all(o[:2] == n[:2] and int(n[2]) > int(o[2]) for o, n in zip(old, new))


def classify(changes: list[FileChange], policy: dict | None = None) -> dict:
    policy = policy or {}
    enabled = set(policy.get("auto_merge_classes") or [])
    protected = PROTECTED_TIER2 + list(policy.get("protected_extra") or [])
    generated = list(policy.get("generated_globs") or [])
    if not changes:
        return {"tier": 1, "class": "", "eligible_class": "", "reasons": ["empty diff"]}
    hits = [ch.path for ch in changes if _matches(ch.path, protected)]
    if hits:
        return {"tier": 2, "class": "", "eligible_class": "", "reasons": [f"protected path: {p}" for p in hits]}
    checks = {
        "docs-only": lambda ch: _is_doc(ch.path) and ch.status != "D",
        "tests-add-only": _is_test_only,
        "lockfile-patch": _is_lock_patch,
        "generated": lambda ch: bool(generated) and _matches(ch.path, generated),
    }
    eligible = next((c for c in CLASSES if all(checks[c](ch) for ch in changes)), "")
    if eligible and eligible in enabled:
        return {"tier": 0, "class": eligible, "eligible_class": eligible, "reasons": [f"all files fit {eligible}"]}
    why = f"fits {eligible} but it is not in auto_merge_classes" if eligible else "ordinary code change"
    return {"tier": 1, "class": "", "eligible_class": eligible, "reasons": [why]}


def _load_policy(path: str) -> dict:
    try:
        import yaml  # type: ignore

        with open(path) as fh:
            return yaml.safe_load(fh) or {}
    except Exception:
        return {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--policy", default=".autopilot/merge-policy.yaml")
    ns = ap.parse_args(argv)
    diff = _git("diff", "-U0", "--no-color", "--no-renames", f"{ns.base}...{ns.head}")
    res = classify(parse_diff(diff), _load_policy(ns.policy))
    print(json.dumps(res))
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as fh:
            fh.write(f"tier={res['tier']}\nclass={res['class']}\neligible_class={res['eligible_class']}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
