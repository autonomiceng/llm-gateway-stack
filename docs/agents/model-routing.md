# Model routing

Routing for every delegation from this repo. Current Owner routing; shared conventions for the four repos (llm-gateway-stack, agent-backplane, observability-stack and platform-edge) live in [docs/conventions.md](../conventions.md).

| Work | Model + effort | Verified by |
| --- | --- | --- |
| Difficult planning or code | gpt-6-astra high | gpt-6-sol high |
| Medium or small work | gpt-6-sol high | gpt-5.6-sol high |
| UI | Claude Opus 5.5 high | gpt-6-astra high |

Rules:

- Use high effort for every delegation. If a model or service is unavailable, use the best available and say so in the report.
- A verifier never sees the implementer's reasoning, only the diff and the brief. Refutation mandate, at most ten findings, each with an exact fix.
- At most two concurrent workers on one host. They share one Docker daemon.
- Launch durable jobs through `devloop agent` (skill: `codex-exec`) and wait in the background. Use the Codex plugin for quick synchronous checks only.
- Report every delegation as task, model, effort.

## Implementation brief template

```text
SLICE <id> - <title>. Workspace: <absolute worktree path>. Branch: <name>.
Read first: AGENTS.md, CONTEXT.md, docs/adr/<binding ADRs>, docs/agents/change-map.md.

VALUE: <one sentence, what becomes observable>.
TOUCHES: <paths>. Do not edit: <paths owned by the orchestrator>.
NON-GOALS: <list>.
TESTS (budget N): 1. <case: defect it detects> ...
DONE WHEN: <exact command and expected output>.
ACCEPTANCE: <exact commands>.
REPORT: deviations, commands run with counts, anything you could not verify.

CODE DISCIPLINE: Make the smallest spec-complete change; add no speculative abstraction and no dependency without approval; match repo idiom and naming; comment only inexpressible constraints; no shims or wrappers around a failing check; report every spec deviation and the exact verification commands and counts.
```

## Review brief template

```text
BLIND REVIEW - slice <id>, <worktree path>, diff <base>..HEAD. Read-only.
Mandate: refutation. Assume the green claims are wrong and try to show it.
Re-run: <acceptance commands>, unshimmed.
Report: at most ten findings, severity, exact fix. Then anything the slice missed that the brief required.
```
