#!/usr/bin/env python3
# ruff: noqa
# (kit-managed file: host-repo lint rules do not apply)
"""Auto-merge for tier-0 PRs. Runs from a trusted workflow on the default branch copy; zero tokens.

Merges PR N only if ALL hold: agent/* or dependabot PR, open, not draft, every other check green, tier.py says tier 0
for a class the owner enabled in merge-policy.yaml (read from the base branch), and that class is not demoted.
Merge method is a merge commit. Marks the merge with label `automerged:<class>` so history (and demotion) can be derived.

  automerge.py --pr N --head-sha SHA --policy merge-policy.yaml [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import demote
import tier
from guard import _git, parse_diff

OK = {"success", "neutral", "skipped"}


def _gh(*a: str):
    out = subprocess.run(["gh", *a], check=True, capture_output=True, text=True).stdout
    return json.loads(out) if out.strip() else []


def history(gh=_gh) -> list[dict]:
    """Merged PRs labelled automerged:<class>, with revert and red-main flags derived from GitHub."""
    prs = gh(
        "pr",
        "list",
        "--state",
        "merged",
        "--label",
        "automerged",
        "--limit",
        "100",
        "--json",
        "number,labels,mergedAt,mergeCommit,title",
    )
    reverts = gh("pr", "list", "--state", "all", "--search", "Revert in:title", "--limit", "100", "--json", "title,body")
    revert_text = " ".join((r.get("title") or "") + " " + (r.get("body") or "") for r in reverts)
    out = []
    for p in prs:
        cls = next((l["name"].split(":", 1)[1] for l in p["labels"] if l["name"].startswith("automerged:")), None)
        if not cls or not p.get("mergedAt"):
            continue
        sha = (p.get("mergeCommit") or {}).get("oid", "")
        checks = (
            gh("api", f"repos/{{owner}}/{{repo}}/commits/{sha}/check-runs", "--jq", "[.check_runs[].conclusion]") if sha else []
        )
        out.append(
            {
                "class": cls,
                "merged_at": p["mergedAt"],
                "sha": sha,
                "reverted": f"#{p['number']}" in revert_text or (sha and sha[:7] in revert_text),
                "main_red": any(c in ("failure", "timed_out") for c in checks),
            }
        )
    return out


def decide(pr: dict, checks: list[dict], res: dict, enabled: list[str], demoted: dict[str, str]) -> tuple[bool, str]:
    if pr["state"] != "OPEN" or pr.get("isDraft"):
        return False, "not an open, ready PR"
    head = pr["headRefName"]
    if not (head.startswith("agent/") or pr["author"]["login"] in ("dependabot", "app/dependabot")):
        return False, "only agent/* and dependabot PRs are auto-merged"
    bad = [c["name"] for c in checks if c.get("conclusion") not in OK]
    if bad:
        return False, f"checks not green: {bad}"
    if res["tier"] != 0:
        return False, f"tier {res['tier']}: {'; '.join(res['reasons'])}"
    cls = res["class"]
    if cls not in enabled:
        return False, f"class {cls} not enabled"
    if cls in demoted:
        return False, f"class {cls} demoted: {demoted[cls]}"
    return True, cls


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument("--head-sha", required=True)
    ap.add_argument("--policy", required=True)
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--dry-run", action="store_true")
    ns = ap.parse_args(argv)
    policy = tier._load_policy(ns.policy)
    enabled = list(policy.get("auto_merge_classes") or [])
    if not enabled:
        print("no auto-merge classes enabled; nothing to do")
        return 0
    pr = _gh("pr", "view", str(ns.pr), "--json", "state,isDraft,headRefName,headRefOid,author")
    if pr["headRefOid"] != ns.head_sha:
        print("head moved since the triggering run; skipping")
        return 0
    rollup = _gh("pr", "view", str(ns.pr), "--json", "statusCheckRollup")["statusCheckRollup"]
    checks = [
        {"name": c.get("name") or c.get("context"), "conclusion": (c.get("conclusion") or "").lower() or "pending"}
        for c in rollup
        if (c.get("name") or "") != "automerge"
    ]
    _git("fetch", "-q", "origin", f"pull/{ns.pr}/head")  # data only: nothing from the PR is executed
    res = tier.classify(parse_diff(_git("diff", "-U0", "--no-color", "--no-renames", f"{ns.base}...FETCH_HEAD")), policy)
    dm = demote.demoted_classes(history())
    ok, why = decide(pr, checks, res, enabled, dm)
    print(("MERGE " if ok else "SKIP ") + why)
    if ok and not ns.dry_run:
        subprocess.run(["gh", "label", "create", "automerged", "--force", "--color", "BFD4F2"], check=False)
        subprocess.run(["gh", "label", "create", f"automerged:{why}", "--force", "--color", "BFD4F2"], check=False)
        subprocess.run(["gh", "pr", "edit", str(ns.pr), "--add-label", f"automerged,automerged:{why}"], check=True)
        subprocess.run(["gh", "pr", "merge", str(ns.pr), "--merge", "--match-head-commit", ns.head_sha], check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
