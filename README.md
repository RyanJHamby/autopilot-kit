# autopilot-kit

Shared, deterministic building blocks for [autopilot](https://github.com): reusable GitHub Actions workflows and the
zero-token scripts they run (`guard`, `gate`, `handshake`, `config`). Consuming repos hold thin callers that pin this
repo by full commit SHA, so a change here reaches a repo only when its owner merges a PR bumping the SHA.

- `.github/workflows/{exec,dispatch,guard,handshake,automerge,improve,audit}.yml`: `workflow_call` workflows
- `runtime/*.py`: stdlib + PyYAML; no network access, no secrets

Contains no tokens or personal configuration. Never reference it by branch or tag; always by commit SHA.

## AI assistance

As of 2026-11-26, one of the 33 non-merge commits on `main`, the initial import, carries a `Co-Authored-By: Claude`
trailer; I reviewed and committed it. The `release:` commits are mirrored here by a bot from the autopilot repository,
so how that code was written is recorded in that repository's history, not this one.
