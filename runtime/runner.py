#!/usr/bin/env python3
# ruff: noqa
# (kit-managed file: host-repo lint rules do not apply)
"""Runner interface and the trusted pipeline around it. Stdlib only.

A Runner is anything that can edit a worktree to satisfy a task: a lab's agent CLI, an open-weights harness, a stub.
It is UNTRUSTED. It never commits, pushes, opens PRs or holds repo credentials. After it returns, trusted code:
  1. diffs the worktree against the base commit (git, not the runner, builds the patch),
  2. runs patchcheck (path/mode/binary/symlink rules + every guard rule),
  3. only then reports the patch as committable. The signed commit and PR are created by the caller with the App/Action
     token, identically for every runner.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from patchcheck import check_patch


@dataclass
class Task:
    issue: int
    prompt: str
    model: str = "default"
    max_turns: int = 25


@dataclass
class Usage:
    """Whatever the runner can report. Subscription runners report nothing; API/open-weights runners report tokens."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    turns: int | None = None


@dataclass
class RunResult:
    summary: str = ""
    usage: Usage = field(default_factory=Usage)


class Runner(Protocol):
    name: str

    def run(self, task: Task, worktree: Path) -> RunResult: ...


@dataclass
class Outcome:
    ok: bool
    patch: str
    violations: list[str]
    summary: str
    usage: Usage


def _git(worktree: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(worktree), *args], check=True, capture_output=True, text=True).stdout


def produce_patch(worktree: Path, base: str = "HEAD") -> str:
    """The patch is built by git from the worktree state, never supplied by the runner. Untracked files count, and the diff
    is against the BASE commit recorded before the run, so a runner that makes its own commits cannot hide changes."""
    _git(worktree, "add", "-A", "-N")  # intent-to-add so new files appear in the diff
    return _git(worktree, "diff", "--binary", "-U0", "--no-color", "--no-renames", base)


def process(runner: Runner, task: Task, worktree: Path, *, protected=None, max_diff_lines=800, comms_allow=None) -> Outcome:
    base = _git(worktree, "rev-parse", "HEAD").strip()
    result = runner.run(task, worktree)
    patch = produce_patch(worktree, base)
    if not patch.strip():
        return Outcome(False, "", ["empty patch: the runner changed nothing"], result.summary, result.usage)
    v = check_patch(patch, protected=protected, max_diff_lines=max_diff_lines, comms_allow=comms_allow)
    return Outcome(not v, patch, [str(x) for x in v], result.summary, result.usage)
