#!/usr/bin/env python3
# ruff: noqa
# (kit-managed file: host-repo lint rules do not apply)
"""Autopilot secret scan: deterministic, zero-token check for high-confidence secret shapes in a PR's added lines.

Run from the pinned kit by the reusable `secrets` workflow. Only ADDED lines of `git diff base...head` are scanned,
plus a file rule for committed `.env` files. A finding names the file, the line and the rule id, never the matched
text. Known false positives go in an allowlist read from the BASE branch (`path-glob:rule-id` per line).
Exit 0 = clean, 1 = findings, 2 = usage error (bad arguments, malformed allowlist, git failure).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from guard import FileChange, _matches, parse_diff  # noqa: E402

# High-confidence shapes only: each has a fixed vendor prefix and a minimum length, so ordinary prose and hashes do not match.
RULES: dict[str, re.Pattern[str]] = {
    "aws-access-key-id": re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
    "github-token": re.compile(
        r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9]{20,}_[A-Za-z0-9]{40,})(?![A-Za-z0-9_])"
    ),
    "anthropic-key": re.compile(r"(?<![A-Za-z0-9_-])sk-ant-[A-Za-z0-9_-]{32,}"),
    "openai-key": re.compile(
        r"(?<![A-Za-z0-9_-])(?:sk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{40,}|sk-[A-Za-z0-9]{48}(?![A-Za-z0-9_-]))"
    ),
    "slack-token": re.compile(r"(?<![A-Za-z0-9])xox[abprs]-[A-Za-z0-9]{6,}-[A-Za-z0-9-]{10,}"),
    "google-api-key": re.compile(r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{35}(?![A-Za-z0-9_-])"),
    "private-key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "stripe-live": re.compile(r"(?<![A-Za-z0-9_])[sr]k_live_[0-9A-Za-z]{24,}(?![A-Za-z0-9_])"),
    "huggingface-token": re.compile(r"(?<![A-Za-z0-9_])hf_[A-Za-z0-9]{34,}(?![A-Za-z0-9_])"),
}
ENV_FILE_RULE = "env-file"
RULE_IDS = frozenset(RULES) | {ENV_FILE_RULE}
ENV_FILE_OK = {".env.example", ".env.sample", ".env.template"}
DEFAULT_ALLOWLIST = ".autopilot/secretscan-allow.txt"


class UsageError(Exception):
    pass


@dataclass(frozen=True)
class Finding:
    path: str
    rule: str
    line: int | None = None

    def __str__(self) -> str:  # never carries the matched value: there is no field for it
        return f"{self.path}:{self.line}: {self.rule}" if self.line else f"{self.path}: {self.rule}"


def is_env_file(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    return (base == ".env" or base.startswith(".env.")) and base not in ENV_FILE_OK


def parse_allowlist(text: str) -> list[tuple[str, str]]:
    """`path-glob:rule-id` per line; `#` starts a comment, blank lines are skipped. Raises UsageError naming the line."""
    out: list[tuple[str, str]] = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        glob, sep, rule = line.rpartition(":")
        glob, rule = glob.strip(), rule.strip()
        if not sep or not glob or rule not in RULE_IDS:
            raise UsageError(f"allowlist line {n}: expected 'path-glob:rule-id' with a known rule id")
        out.append((glob, rule))
    return out


def load_allowlist(path: str | None) -> list[tuple[str, str]]:
    """A missing file is an empty allowlist."""
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return parse_allowlist(fh.read())


def _allowed(f: Finding, allow: list[tuple[str, str]]) -> bool:
    return any(rule == f.rule and _matches(f.path, [glob]) for glob, rule in allow)


def scan(changes: list[FileChange], allow: list[tuple[str, str]] | None = None) -> list[Finding]:
    allow = allow or []
    out: list[Finding] = []
    for ch in changes:
        if ch.status == "A" and is_env_file(ch.path):
            out.append(Finding(ch.path, ENV_FILE_RULE))
        for i, text in enumerate(ch.added):
            n = ch.added_at[i] if i < len(ch.added_at) else None
            for rule, rx in RULES.items():
                if rx.search(text):
                    out.append(Finding(ch.path, rule, n))
    return [f for f in out if not _allowed(f, allow)]


def _diff(base: str, head: str) -> str:
    r = subprocess.run(
        ["git", "diff", "-U0", "--no-color", "--no-renames", "--no-ext-diff", "--no-textconv", f"{base}...{head}"],
        capture_output=True,
        text=True,
        errors="replace",
    )
    if r.returncode != 0:
        raise UsageError(f"git diff {base}...{head} failed: {r.stderr.strip().splitlines()[-1:] or ['?']}")
    return r.stdout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", required=True, help="base ref, e.g. origin/main")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--allowlist", default=DEFAULT_ALLOWLIST, help="path-glob:rule-id file (the workflow passes the base-branch copy)")
    ns = ap.parse_args(argv)  # argparse exits 2 on bad arguments
    try:
        allow = load_allowlist(ns.allowlist)
        findings = scan(parse_diff(_diff(ns.base, ns.head)), allow)
    except UsageError as e:
        print(f"autopilot secretscan: usage error: {e}", file=sys.stderr)
        return 2
    if not findings:
        print("autopilot secretscan: clean")
        return 0
    print("autopilot secretscan: FAILED (matched values are not shown)")
    for f in findings:
        print(f"  {f}")
        print(f"::error file={f.path}{f',line={f.line}' if f.line else ''}::secretscan: {f.rule}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
