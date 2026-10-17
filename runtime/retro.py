#!/usr/bin/env python3
# ruff: noqa
# (kit-managed file: host-repo lint rules do not apply)
"""Failure retro evidence: classify why agent work did not land. Pure functions; no network, no model.

The evidence packet is what a cheap model (or the owner) reads to write a lesson. Counting causes first means lessons
target what actually keeps failing instead of whatever the last failure was.
"""

from __future__ import annotations

import re
from collections import Counter
import argparse
import datetime as dt
import json
import os
import subprocess
import sys

GUARD_RE = re.compile(
    r"\[(secret|comms|protected-path|mcp-config|test-deletion|test-weakening|size|path|symlink|mode|binary|rename)\]"
)


def classify_run(run: dict) -> str | None:
    """run: {conclusion, failed_step?, guard_output?, pr_opened?}. None = a success worth ignoring."""
    c = run.get("conclusion")
    if c in ("success", "skipped", "cancelled", None, ""):
        return None
    if c == "timed_out":
        return "timeout"
    m = GUARD_RE.search(run.get("guard_output") or "")
    if m:
        return f"guard:{m.group(1)}"
    step = (run.get("failed_step") or "").lower()
    if "claude" in step or "anthropic" in step:
        return "agent-error"
    if step:
        return f"infra:{re.sub(r'[^a-z0-9]+', '-', step).strip('-')[:40]}"
    return "failed-unknown"


def classify_pr(pr: dict) -> str | None:
    """pr: {state, mergedAt, ci?: green|red, guard_output?, comments?: int}. Closed-unmerged agent PRs only."""
    if pr.get("mergedAt") or pr.get("state") != "CLOSED":
        return None
    m = GUARD_RE.search(pr.get("guard_output") or "")
    if m:
        return f"guard:{m.group(1)}"
    if pr.get("ci") == "red":
        return "ci-red"
    return "rejected-by-owner" if (pr.get("comments") or 0) > 0 else "closed-no-reason"


def causes(runs: list[dict], prs: list[dict]) -> Counter:
    out: Counter = Counter()
    for r in runs:
        k = classify_run(r)
        if k:
            out[k] += 1
    for p in prs:
        k = classify_pr(p)
        if k:
            out[k] += 1
    return out


def packet(counter: Counter, examples: dict[str, list[str]], *, min_count: int = 2) -> str:
    """Markdown the lesson-writer reads. Only causes seen at least min_count times become lesson candidates."""
    lines = ["# Failure retro evidence", ""]
    if not counter:
        return "\n".join(lines + ["No failures in the window.", ""])
    lines += ["| cause | count | lesson candidate |", "|---|---|---|"]
    for cause, n in counter.most_common():
        lines.append(f"| {cause} | {n} | {'yes' if n >= min_count else 'no (seen once)'} |")
    lines.append("")
    for cause, n in counter.most_common():
        if n >= min_count and examples.get(cause):
            lines += [f"## {cause}", *[f"- {e}" for e in examples[cause][:5]], ""]
    lines.append("Lessons go in `.autopilot/lessons.md` through a PR the owner reviews. Never loosen a guardrail in a lesson.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- gathering (REST only) and lesson validation


def _gh(*a: str):
    r = subprocess.run(["gh", *a], capture_output=True, text=True)
    return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else None


def gather(days: int = 14) -> tuple[Counter, dict[str, list[str]]]:
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    ts = lambda s: dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    runs, prs, ex = [], [], {}
    data = _gh("api", "repos/{owner}/{repo}/actions/workflows/agent-exec.yml/runs?per_page=50") or {}
    for r in data.get("workflow_runs", []):
        if ts(r["created_at"]) < since or r.get("conclusion") in (None, "success", "skipped", "cancelled", ""):
            continue
        jobs = _gh("api", f"repos/{{owner}}/{{repo}}/actions/runs/{r['id']}/jobs") or {}
        step = ""
        for j in jobs.get("jobs", []):
            step = next((s["name"] for s in j.get("steps", []) if s.get("conclusion") == "failure"), step)
        item = {"conclusion": r["conclusion"], "failed_step": step}
        runs.append(item)
        ex.setdefault(classify_run(item) or "", []).append(f"run {r['id']} ({(r.get('display_title') or '').strip()})")
    for p in _gh("api", "repos/{owner}/{repo}/pulls?state=closed&per_page=50") or []:
        if (
            not p["head"]["ref"].startswith("agent/")
            or p.get("merged_at")
            or not p.get("closed_at")
            or ts(p["closed_at"]) < since
        ):
            continue
        n = (_gh("api", f"repos/{{owner}}/{{repo}}/issues/{p['number']}") or {}).get("comments", 0)
        item = {"state": "CLOSED", "mergedAt": None, "comments": n}
        prs.append(item)
        ex.setdefault(classify_pr(item) or "", []).append(f"PR #{p['number']} ({p['title']})")
    return causes(runs, prs), ex


_BAD = [
    (r"https?://", "contains a URL"),
    (r"`{3}", "contains a code fence"),
    (r"(?i)\b(ignore|disregard|override)\b.{0,30}\b(rules?|instructions?|constitution|guard)", "tries to override the rules"),
    (
        r"(?i)\b(disable|loosen|bypass|skip|weaken|remove)\b.{0,30}\b(guard|check|test|protect|sandbox|scrub|signing|ruleset|review|constitution)",
        "suggests weakening a safeguard",
    ),
    (r"(?i)\b(secret|token|password|api[ _-]?key|credential)", "mentions secrets"),
    (
        r"(?i)\b(email|slack|post|publish|message|notify|contact)\b.{0,30}\b(someone|people|user|customer|team|channel)",
        "suggests contacting people",
    ),
    (r"(?i)dangerouslyDisableSandbox|--no-verify|force[- ]push", "suggests an unsafe command"),
]


def validate_lessons(text: str, max_chars: int = 1200, max_bullets: int = 5) -> tuple[bool, list[str]]:
    """A lesson is an instruction to future agents, so it is held to a narrow shape: a few short imperative bullets about how
    to do the work, nothing that weakens a safeguard, contacts anyone, or carries a link or code."""
    problems: list[str] = []
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return False, ["empty"]
    if len(text) > max_chars:
        problems.append(f"over {max_chars} characters")
    if len(lines) > max_bullets or any(not ln.lstrip().startswith("- ") for ln in lines):
        problems.append(f"must be at most {max_bullets} lines, each starting with '- '")
    if any(len(ln) > 220 for ln in lines):
        problems.append("a bullet is longer than 220 characters")
    for rx, why in _BAD:
        if re.search(rx, text):
            problems.append(why)
    return not problems, problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="failure-cause evidence for this repo (REST only)")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--out", required=True, help="write the markdown evidence packet here")
    ap.add_argument("--min-count", type=int, default=2)
    ns = ap.parse_args(argv)
    counter, ex = gather(ns.days)
    open(ns.out, "w").write(packet(counter, ex, min_count=ns.min_count))
    has = any(n >= ns.min_count for n in counter.values())
    print(f"causes: {dict(counter)}; lesson candidates: {has}")
    dest = os.environ.get("GITHUB_OUTPUT")
    if dest:
        with open(dest, "a") as fh:
            fh.write(f"has_candidates={'true' if has else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
