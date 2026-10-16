# autopilot-kit

Shared, deterministic building blocks for [autopilot](https://github.com): reusable GitHub Actions workflows and the
zero-token scripts they run (`guard`, `gate`, `handshake`, `config`). Consuming repos hold thin callers that pin this
repo by full commit SHA, so a change here reaches a repo only when its owner merges a PR bumping the SHA.

- `.github/workflows/{exec,dispatch,guard,handshake,automerge}.yml`: `workflow_call` workflows
- `runtime/*.py`: stdlib + PyYAML; no network access, no secrets

Contains no tokens or personal configuration. Never reference it by branch or tag; always by commit SHA.
