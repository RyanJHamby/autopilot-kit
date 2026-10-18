#!/usr/bin/env python3
# ruff: noqa
# (kit-managed file: host-repo lint rules do not apply)
"""Per-repo charter: the bounds an unattended agent works inside. Pure functions; no network, no model.

The charter lives under `charter:` in .autopilot/repo.yaml (a protected path: agents cannot edit it, loosening it is your PR).
  goals: [{id: G1, text: ...}]      what the repo is for; the weekly audit may only propose work that serves one
  non_goals: [..]                   phrases that make a proposal (and the implementer) stay away
  allow_new_dependencies: false     guard fails agent PRs that add a package
  weekly_lines_budget: 1500         gate stops dispatch once merged agent PRs changed this many lines in 7 days
  proposals_per_week: 3             audit files at most this many issues
  backlog_max: 6                    audit files nothing while this many agent-ready issues are already open
  auto_ready: true                  valid proposals are labeled agent-ready; false = `proposed` and wait for you
"""

from __future__ import annotations

import json
import re
import sys

DEFAULTS = {
    "goals": [],
    "non_goals": [],
    "allow_new_dependencies": False,
    "weekly_lines_budget": None,
    "proposals_per_week": 3,
    "backlog_max": 6,
    "auto_ready": False,
}


def load(cfg: dict) -> dict:
    c = dict(DEFAULTS)
    c.update((cfg or {}).get("charter") or {})
    return c


# ---- dependency guard -------------------------------------------------------------------------------------------------

_PKG_JSON = re.compile(r'^\s*"(@?[\w./-]+)"\s*:\s*"[\^~<>=*]*\d[^"]*"\s*,?\s*$')
_REQ_TXT = re.compile(r"^([A-Za-z0-9_.-]+)(\[[\w,]+\])?\s*([<>=~!]=?.*)?$")
_PYPROJECT = re.compile(r'^\s*"([A-Za-z0-9_.-]+)(\[[\w,]+\])?\s*([<>=~!^]=?[^"]*)?"\s*,?\s*$')


def _names(lines: list[str], rx: re.Pattern, skip=()) -> set[str]:
    out = set()
    for ln in lines:
        m = rx.match(ln.strip() if rx is _REQ_TXT else ln)
        if m and m.group(1) not in skip:
            out.add(m.group(1).lower())
    return out


def added_dependencies(path: str, added: list[str], removed: list[str]) -> set[str]:
    """Package names an added-lines diff introduces (version bumps of an existing name do not count). Heuristic by design:
    a false positive sends the PR to the owner, a false negative is still caught by review of tier-1 code."""
    base = path.rsplit("/", 1)[-1]
    if base == "package.json":
        new, old = _names(added, _PKG_JSON, skip=("version",)), _names(removed, _PKG_JSON, skip=("version",))
    elif base.startswith("requirements") and base.endswith(".txt"):
        new, old = _names(added, _REQ_TXT), _names(removed, _REQ_TXT)
    elif base == "pyproject.toml":
        new, old = _names(added, _PYPROJECT), _names(removed, _PYPROJECT)
    else:
        return set()
    return new - old


# ---- weekly lines budget ----------------------------------------------------------------------------------------------


def lines_used(pulls: list[dict], now, fetch_size, days: int = 7) -> int:
    """Changed lines in agent PRs merged in the last `days`. `pulls` = closed PR list; `fetch_size(n)` -> {additions, deletions}."""
    import datetime as dt

    since = now - dt.timedelta(days=days)
    total = 0
    for p in pulls:
        m = p.get("merged_at")
        if not m or not str((p.get("head") or {}).get("ref", "")).startswith("agent/"):
            continue
        if dt.datetime.fromisoformat(m.replace("Z", "+00:00")) < since:
            continue
        d = fetch_size(p["number"])
        total += int(d.get("additions", 0)) + int(d.get("deletions", 0))
    return total


# ---- audit proposals --------------------------------------------------------------------------------------------------

_URL = re.compile(r"https?://|www\.", re.I)
_SECRETY = re.compile(r"(?i)sk-[a-z0-9]{16,}|ghp_|api[_-]?key\s*[:=]")


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def validate_proposals(raw: str, charter: dict, open_titles: list[str], backlog: int) -> tuple[list[dict], list[str]]:
    """-> (accepted proposals, reasons for rejections). The model only writes JSON; this decides what becomes an issue."""
    try:
        items = json.loads(raw)
    except Exception:
        return [], ["proposals file is not valid JSON"]
    if not isinstance(items, list):
        return [], ["proposals must be a JSON list"]
    room = max(0, int(charter["backlog_max"]) - backlog)
    cap = min(int(charter["proposals_per_week"]), room)
    goals = {g.get("id") for g in charter["goals"] if isinstance(g, dict)}
    non_goals = [_norm(n) for n in charter["non_goals"] if _norm(n)]
    seen = {_norm(t) for t in open_titles}
    ok: list[dict] = []
    why: list[str] = []
    if cap == 0:
        return [], [f"no room: backlog {backlog}/{charter['backlog_max']}"]
    for it in items:
        if len(ok) >= cap:
            why.append("over the weekly cap")
            break
        if not isinstance(it, dict):
            why.append("non-object entry")
            continue
        t, g, b = str(it.get("title", "")).strip(), it.get("goal"), str(it.get("body", "")).strip()
        text = _norm(t + " " + b)
        if not 10 <= len(t) <= 120:
            why.append(f"bad title length: {t[:40]!r}")
        elif g not in goals:
            why.append(f"unknown goal {g!r}: {t[:40]!r}")
        elif not 40 <= len(b) <= 1500:
            why.append(f"body length out of range: {t[:40]!r}")
        elif _URL.search(t + b) or _SECRETY.search(t + b):
            why.append(f"links or secrets not allowed: {t[:40]!r}")
        elif any(n in text for n in non_goals):
            why.append(f"touches a non-goal: {t[:40]!r}")
        elif _norm(t) in seen:
            why.append(f"duplicate of an open issue: {t[:40]!r}")
        else:
            seen.add(_norm(t))
            ok.append({"title": t, "goal": g, "body": b})
    return ok, why


def main(argv=None) -> int:
    import argparse, os, subprocess, yaml

    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=".autopilot/repo.yaml")
    ap.add_argument("--proposals", default=".autopilot-tmp/proposals.json")
    ap.add_argument("--apply", action="store_true")
    ns = ap.parse_args(argv)
    ch = load(yaml.safe_load(open(ns.config)) or {})
    if not ch["goals"]:
        print("no charter goals; the audit has nothing to serve")
        return 0

    def gh(*a):
        return subprocess.run(["gh", *a], check=True, capture_output=True, text=True).stdout

    # REST (the workflow token cannot run the GraphQL behind `gh issue list`); the issues endpoint also returns PRs
    raw_issues = json.loads(gh("api", "repos/{owner}/{repo}/issues?state=all&per_page=100"))
    issues = [
        {"title": i["title"], "state": i["state"].upper(), "labels": i["labels"]} for i in raw_issues if "pull_request" not in i
    ]
    open_titles = [i["title"] for i in issues]
    backlog = sum(1 for i in issues if i["state"] == "OPEN" and any(l["name"] == "agent-ready" for l in i["labels"]))
    raw = open(ns.proposals).read() if os.path.exists(ns.proposals) else "[]"
    ok, why = validate_proposals(raw, ch, open_titles, backlog)
    for w in why:
        print(f"rejected: {w}")
    label = "agent-ready" if ch["auto_ready"] else "proposed"
    for p in ok:
        print(f"{'FILE' if ns.apply else 'would file'} [{label}] {p['title']} (goal {p['goal']})")
        if ns.apply:
            for lab in ("audit", label):
                subprocess.run(
                    ["gh", "api", "repos/{owner}/{repo}/labels", "-f", f"name={lab}", "-f", "color=BFD4F2"], capture_output=True
                )
            body = f"{p['body']}\n\n---\nGoal: {p['goal']}. Filed by the weekly audit within the repo charter; close it if it is not wanted."
            gh(
                "api",
                "repos/{owner}/{repo}/issues",
                "-f",
                f"title={p['title']}",
                "-f",
                f"body={body}",
                "-f",
                "labels[]=audit",
                "-f",
                f"labels[]={label}",
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
