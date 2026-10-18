#!/usr/bin/env python3
# ruff: noqa
# (kit-managed file: host-repo lint rules do not apply)
"""Auto-merge for tier-0 PRs. Runs from a trusted workflow on the default branch copy; zero tokens.

Merges PR N only if ALL hold: agent/*, dependabot or owner `autopilot/kit-*` PR, open, not draft, every other check green, tier.py says tier 0
for a class the owner enabled in merge-policy.yaml (read from the base branch), and that class is not demoted.
Merge method is a merge commit. Marks the merge with label `automerged:<class>` so history (and demotion) can be derived.

  automerge.py --pr N --head-sha SHA --policy merge-policy.yaml [--dry-run]
"""

from __future__ import annotations

import argparse
import base64
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
    r = subprocess.run(["gh", *a], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"gh {' '.join(a[:3])} failed: {r.stderr.strip()[:300]}")
    return json.loads(r.stdout) if r.stdout.strip() else []


def history(gh=None) -> list[dict]:
    """Merged PRs labelled automerged:<class>, with revert and red-main flags derived from GitHub (REST only: the
    workflow token lacks GraphQL status scopes)."""
    gh = gh or _gh
    prs = gh("api", "repos/{owner}/{repo}/pulls?state=closed&per_page=100&sort=updated&direction=desc")
    revert_text = " ".join(
        (p.get("title") or "") + " " + (p.get("body") or "") for p in prs if (p.get("title") or "").startswith("Revert")
    )
    out = []
    for p in prs:
        cls = next((l["name"].split(":", 1)[1] for l in p.get("labels", []) if l["name"].startswith("automerged:")), None)
        if not cls or not p.get("merged_at"):
            continue
        sha = p.get("merge_commit_sha") or ""
        runs = gh("api", f"repos/{{owner}}/{{repo}}/commits/{sha}/check-runs") if sha else {}
        concl = [c.get("conclusion") for c in (runs.get("check_runs", []) if isinstance(runs, dict) else [])]
        out.append(
            {
                "class": cls,
                "merged_at": p["merged_at"],
                "sha": sha,
                "reverted": f"#{p['number']}" in revert_text or (bool(sha) and sha[:7] in revert_text),
                "main_red": any(c in ("failure", "timed_out") for c in concl),
            }
        )
    return out


def approvals_repo(kit_repo: str) -> str:
    """The owner's approvals live in a separate tiny repo `<owner>/autopilot-approved`, not in the kit repo, because the kit
    repo is pushed to constantly (releases) while approvals must only ever change through an owner-reviewed PR."""
    return kit_repo.split("/")[0] + "/autopilot-approved"


def fetch_approved(kit_repo: str, opener=None) -> list[str]:
    """Kit shas the owner approved: `APPROVED` on the approvals repo's default branch (ruleset: PR + owner review).
    Any failure returns [] so the kit-bump class fails closed."""
    import re
    import urllib.request

    if not re.fullmatch(r"[\w.-]+/[\w.-]+", kit_repo or ""):
        return []
    try:

        def default(u: str) -> str:
            # GitHub's contents API, not raw.githubusercontent.com: the raw CDN caches for minutes, so a fresh approval (or a
            # revoked one) would not be seen promptly. A token (when present) only lifts the anonymous rate limit.
            h = {"Accept": "application/vnd.github.raw+json", "User-Agent": "autopilot-kit"}
            tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
            if tok:
                h["Authorization"] = f"Bearer {tok}"
            return urllib.request.urlopen(urllib.request.Request(u, headers=h), timeout=10).read().decode()

        opener = opener or default
        text = opener(f"https://api.github.com/repos/{approvals_repo(kit_repo)}/contents/APPROVED?ref=main")
    except Exception:
        return []
    return [ln.split()[0] for ln in text.splitlines() if re.match(r"^[0-9a-f]{40}(\s|$)", ln)]


def decide(pr: dict, checks: list[dict], res: dict, enabled: list[str], demoted: dict[str, str]) -> tuple[bool, str]:
    if pr["state"] != "OPEN" or pr.get("isDraft"):
        return False, "not an open, ready PR"
    head = pr["headRefName"]
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    kit_bump = head.startswith("autopilot/kit-") and bool(owner) and pr["author"]["login"] == owner
    if not (head.startswith("agent/") or kit_bump or pr["author"]["login"] in ("dependabot", "app/dependabot")):
        return False, "only agent/*, dependabot and owner kit-bump PRs are auto-merged"
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
    raw = _gh("api", f"repos/{{owner}}/{{repo}}/pulls/{ns.pr}")
    pr = {
        "state": raw["state"].upper(),
        "isDraft": raw["draft"],
        "headRefName": raw["head"]["ref"],
        "headRefOid": raw["head"]["sha"],
        "author": {"login": raw["user"]["login"]},
    }
    if pr["headRefOid"] != ns.head_sha:
        print("head moved since the triggering run; skipping")
        return 0
    # REST check-runs on the head sha: `gh pr view --json statusCheckRollup` is GraphQL and the workflow token cannot read it
    runs = _gh("api", f"repos/{{owner}}/{{repo}}/commits/{ns.head_sha}/check-runs?per_page=100")["check_runs"]
    checks = [
        {"name": c["name"], "conclusion": c.get("conclusion") or "pending"} for c in runs if not c["name"].startswith("automerge")
    ]
    if not checks:
        print("SKIP no checks reported yet")
        return 0
    # data only: nothing from the PR is executed. The checkout keeps no credentials, so private repos need the token here.
    tok = base64.b64encode(f"x-access-token:{os.environ.get('GH_TOKEN', '')}".encode()).decode()
    try:
        _git("-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {tok}", "fetch", "-q", "origin", f"pull/{ns.pr}/head")
    except subprocess.CalledProcessError as e:
        print(f"SKIP fetch failed: {e.stderr.strip()[:300]}")
        return 0
    policy = {**policy, "kit_approved": fetch_approved(os.environ.get("KIT_REPO", ""))}
    res = tier.classify(parse_diff(_git("diff", "-U0", "--no-color", "--no-renames", f"{ns.base}...FETCH_HEAD")), policy)
    dm = demote.demoted_classes(history())
    ok, why = decide(pr, checks, res, enabled, dm)
    print(("MERGE " if ok else "SKIP ") + why)
    if ok and not ns.dry_run:
        # labels are the history demotion is derived from, so add them BEFORE merging
        for lab in ("automerged", f"automerged:{why}"):
            subprocess.run(
                ["gh", "api", "repos/{owner}/{repo}/labels", "-f", f"name={lab}", "-f", "color=BFD4F2"], capture_output=True
            )
        _gh(
            "api",
            f"repos/{{owner}}/{{repo}}/issues/{ns.pr}/labels",
            "-f",
            "labels[]=automerged",
            "-f",
            f"labels[]=automerged:{why}",
        )
        _gh(
            "api",
            "-X",
            "PUT",
            f"repos/{{owner}}/{{repo}}/pulls/{ns.pr}/merge",
            "-f",
            "merge_method=merge",
            "-f",
            f"sha={ns.head_sha}",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
