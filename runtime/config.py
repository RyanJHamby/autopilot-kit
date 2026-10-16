#!/usr/bin/env python3
# ruff: noqa
# (kit-managed file: host-repo lint rules do not apply)
"""Reads .autopilot/repo.yaml (and the repo's lockfiles) on the default branch and emits the runner setup the exec
workflow needs, as GITHUB_OUTPUT lines. Zero tokens. The agent never influences this: it runs before the model starts.

  node_version   set when package-lock.json exists (explicit `stack.node` in repo.yaml wins, then .nvmrc, then 20)
  uv             "true" when uv.lock and pyproject.toml exist (the exec job installs uv and runs `uv sync --frozen`)
  allowed_tools  comma-separated --allowedTools list: BASE_TOOLS plus Bash(<cmd>:*) for each `commands:` entry
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

BASE_TOOLS = [
    "Edit",
    "Write",
    "Read",
    "Glob",
    "Grep",
    "Bash(git add:*)",
    "mcp__github_file_ops__commit_files",  # signed commits via the GitHub API; plain git commit is intentionally absent
    "Bash(git status:*)",
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(gh pr create:*)",
    "Bash(gh pr view:*)",
    "Bash(gh issue view:*)",
    "Bash(gh issue comment:*)",
    "Bash(gh issue edit:*)",
]
SAFE_CMD = re.compile(r"^[A-Za-z0-9._:/@=+ -]+$")  # no quotes, commas, parens or shell metacharacters


def node_version(repo: Path, cfg: dict) -> str:
    if not (repo / "package-lock.json").exists():
        return ""
    explicit = str((cfg.get("stack") or {}).get("node") or "")
    if explicit:
        if not re.fullmatch(r"\d{1,2}(\.\d+){0,2}", explicit):
            raise SystemExit(f"stack.node {explicit!r} is not a version")
        return explicit
    nvmrc = repo / ".nvmrc"
    if nvmrc.exists():
        m = re.search(r"\d+", nvmrc.read_text())
        if m:
            return m.group(0)
    return "20"


def uses_uv(repo: Path) -> str:
    return "true" if (repo / "uv.lock").exists() and (repo / "pyproject.toml").exists() else ""


def allowed_tools(cfg: dict) -> str:
    tools = list(BASE_TOOLS)
    for c in cfg.get("commands") or []:
        c = str(c).strip()
        if not SAFE_CMD.match(c):
            raise SystemExit(f"command {c!r} in repo.yaml has characters that are not allowed in a tool rule")
        tools.append(f"Bash({c}:*)")
    return ",".join(tools)


def main(argv: list[str] | None = None) -> int:
    import yaml  # type: ignore

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--config", default=".autopilot/repo.yaml")
    ns = ap.parse_args(argv)
    repo = Path(ns.repo)
    cfg = yaml.safe_load((repo / ns.config).read_text()) or {}
    out = {"node_version": node_version(repo, cfg), "uv": uses_uv(repo), "allowed_tools": allowed_tools(cfg)}
    for k, v in out.items():
        print(f"{k}={v}")
    dest = os.environ.get("GITHUB_OUTPUT")
    if dest:
        with open(dest, "a") as fh:
            for k, v in out.items():
                fh.write(f"{k}={v}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
