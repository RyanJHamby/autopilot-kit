#!/usr/bin/env python3
# ruff: noqa
# (kit-managed file: host-repo lint rules do not apply)
"""Keep open agent PRs mergeable without the owner. Zero tokens in the common case.

For each open PR from an `agent/*` branch (REST only): `behind` -> PUT update-branch (GitHub-signed merge of the default
branch, no checkout); `dirty` (conflict) -> label `needs-rebase` and re-dispatch the implementer for its issue through the
normal gate (so every gate cap, hold label and skip_merged still applies). At most one re-dispatch per PR per day.
PRs not from `agent/*` are never touched. A PR touching a protected path only ever gets update-branch.

  rebase.py [--config .autopilot/repo.yaml] [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gate
import tier
from guard import _matches

NOOP, UPDATE, REDISPATCH = "noop", "update-branch", "redispatch"


def issue_of(branch: str) -> int | None:
    m = re.match(r"agent/(\d+)-", branch or "")
    return int(m.group(1)) if m else None


def decide(pr: dict) -> str:
    """pr: head_ref, mergeable_state, protected (bool), labels (set), attempted_today, inflight, merged (bools)."""
    if issue_of(pr.get("head_ref", "")) is None:
        return NOOP
    state = pr.get("mergeable_state")
    if state == "behind":
        return UPDATE
    if state != "dirty" or pr.get("protected"):
        return NOOP
    if pr.get("attempted_today") or pr.get("inflight") or pr.get("merged"):
        return NOOP
    if set(pr.get("labels") or ()) & gate.SKIP_LABELS:
        return NOOP
    return REDISPATCH


def _is_protected(files: list[dict]) -> bool:
    return any(_matches(f.get("filename", ""), tier.PROTECTED_TIER2) for f in files)


def run(cfg: dict, *, gh=None, dispatch=None, now=None, dry_run: bool = False) -> list[tuple[int, str]]:
    gh = gh or gate._gh
    dispatch = dispatch or gate.cmd_dispatch
    now = now or dt.datetime.now(dt.timezone.utc)
    issues = gh("api", "repos/{owner}/{repo}/issues?state=open&per_page=100")
    runs = gh("run", "list", "--workflow", "agent-exec.yml", "--limit", "50", "--json", "createdAt,status,displayTitle")
    inflight = gate.inflight_issues(runs)
    day_ago = now - dt.timedelta(hours=24)
    tried = set()
    for r in runs:
        m = re.search(r"agent #(\d+)", r.get("displayTitle") or "")
        if m and dt.datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00")) >= day_ago:
            tried.add(int(m.group(1)))
    merged = None
    done = []
    for it in issues:
        if "pull_request" not in it:
            continue
        n = it["number"]
        pr = gh("api", f"repos/{{owner}}/{{repo}}/pulls/{n}")
        ref = (pr.get("head") or {}).get("ref") or ""
        issue = issue_of(ref)
        if issue is None:
            continue
        labels = {l["name"] for l in it.get("labels", [])}
        state = pr.get("mergeable_state")
        protected = False
        if state == "dirty":
            protected = _is_protected(gh("api", f"repos/{{owner}}/{{repo}}/pulls/{n}/files?per_page=100"))
            if merged is None:
                merged = gate.merged_agent_issues(gh("api", "repos/{owner}/{repo}/pulls?state=closed&per_page=100"))
        act = decide(
            {
                "head_ref": ref,
                "mergeable_state": state,
                "protected": protected,
                "labels": labels,
                "attempted_today": issue in tried,
                "inflight": issue in inflight,
                "merged": gate.skip_merged(issue, labels, merged or set()),
            }
        )
        done.append((n, act))
        print(f"PR #{n}: {state} -> {act}")
        if dry_run or act == NOOP:
            continue
        if act == UPDATE:
            subprocess.run(["gh", "api", "-X", "PUT", f"repos/{{owner}}/{{repo}}/pulls/{n}/update-branch"], check=False)
        else:
            subprocess.run(["gh", "pr", "edit", str(n), "--add-label", "needs-rebase"], check=False)
            dispatch(cfg, issue)
    return done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=".autopilot/repo.yaml")
    ap.add_argument("--dry-run", action="store_true")
    ns = ap.parse_args(argv)
    run(gate._load_cfg(ns.config), dry_run=ns.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
