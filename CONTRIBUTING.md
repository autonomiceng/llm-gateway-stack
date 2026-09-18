# Contributing

Read [AGENTS.md](AGENTS.md), [CONTEXT.md](CONTEXT.md) and the [design](docs/DESIGN.md) before changing anything. [ADRs](docs/adr/) hold the decisions and why. Conventions shared by the four repos live in [docs/conventions.md](docs/conventions.md).

Toolchain is pinned in `mise.toml`; run `mise install` once. Keep changes focused, use Conventional Commits, and explain the problem and resulting behavior in your pull request.

Gates, from the repository root:

```sh
scripts/validate.sh          # env render, compose config, Caddyfile, shellcheck, py_compile
python3 -m unittest discover -s tests
scripts/smoke.sh             # full boot on this host; needs Docker and ~6 GB of images
```

CI runs the first two on every push and the smoke contract and two-cycle recovery drill on every PR, weekly, and on demand. A version bump merges only when the smoke contract passes on the new pins.

Report bugs and proposals through the issue templates. Report vulnerabilities through the [security policy](SECURITY.md).
