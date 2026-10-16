#!/usr/bin/env python3
# ruff: noqa
# (kit-managed file: host-repo lint rules do not apply)
"""Patch gate for the vendor-neutral commit path. Zero tokens, stdlib only.

Any runner (a lab's agent, an open-weights harness) only edits a worktree; a TRUSTED job turns that into a patch and runs
this before creating a signed commit. So the commit path is identical for every runner and does not depend on a
runner's own signing or sandbox. Rejects what guard cannot see in a diff body: symlinks, mode changes, binary blobs,
renames, path tricks and .git internals; then applies the same guard rules (protected paths, secrets, comms, tests, size).

  patchcheck.py --patch FILE [--config .autopilot/repo.yaml]    exit 0 clean, 1 violations
"""

from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from guard import DEFAULT_MAX_DIFF_LINES, DEFAULT_PROTECTED, Violation, analyze, parse_diff

_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$")


def check_patch(
    text: str, *, protected: list[str] | None = None, max_diff_lines: int = DEFAULT_MAX_DIFF_LINES, comms_allow=None
) -> list[Violation]:
    out: list[Violation] = []
    if "\x00" in text:
        out.append(Violation("patch-format", "(patch)", "NUL byte in patch"))
    cur = "(unknown)"
    for line in text.splitlines():
        m = _HEADER.match(line)
        if m:
            a, b = m.group(1), m.group(2)
            cur = b
            for p in (a, b):
                parts = p.split("/")
                if p.startswith("/") or ".." in parts or ".git" in parts or p.startswith("~"):
                    out.append(Violation("path", p, "absolute, parent-relative or .git path"))
            if a != b:
                out.append(Violation("rename", cur, "renames/copies are not accepted (delete + add, so guard sees both paths)"))
        elif re.match(r"^(new|deleted) file mode (120000|160000)", line):
            kind = "symlink" if "120000" in line else "submodule"
            out.append(Violation(kind, cur, f"{kind} change is not accepted"))
        elif re.match(r"^(old|new) mode ", line) or re.match(r"^new file mode 100[75]55", line):
            out.append(Violation("mode", cur, "file mode / executable bit change is not accepted"))
        elif line.startswith(("GIT binary patch", "Binary files ")):
            out.append(Violation("binary", cur, "binary change is not accepted"))
        elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            out.append(Violation("rename", cur, "renames/copies are not accepted"))
    out += analyze(
        parse_diff(text),
        agent=True,
        protected=protected if protected is not None else DEFAULT_PROTECTED,
        max_diff_lines=max_diff_lines,
        comms_allow=comms_allow or [],
    )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--patch", required=True)
    ap.add_argument("--config", default=".autopilot/repo.yaml")
    ns = ap.parse_args(argv)
    cfg = {}
    try:
        import yaml  # type: ignore

        cfg = yaml.safe_load(open(ns.config)) or {}
    except Exception:
        pass
    g = cfg.get("guard", {}) or {}
    v = check_patch(
        open(ns.patch, encoding="utf-8", errors="replace").read(),
        protected=DEFAULT_PROTECTED + list(g.get("extra_protected", []) or []),
        max_diff_lines=int(g.get("max_diff_lines", DEFAULT_MAX_DIFF_LINES)),
        comms_allow=list(g.get("comms_allow", []) or []),
    )
    if not v:
        print("patchcheck: clean")
        return 0
    print("patchcheck: REJECTED")
    for x in v:
        print(f"  - {x}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
