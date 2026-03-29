# Security Notice

## Upstream LiteLLM supply-chain incident

LiteLLM was publicly reported as the target of a PyPI supply-chain compromise in March 2026, including compromised releases `1.82.7` and `1.82.8`.

This repository does **not** use those affected PyPI releases by default. The stack pulls a prebuilt LiteLLM container image pinned in `.env.example`. If you override that pin or install LiteLLM separately, verify package provenance carefully and avoid the affected releases.

## References

- [FutureSearch incident transcript](https://futuresearch.ai/blog/litellm-attack-transcript/)
- [LiteLLM GitHub security page](https://github.com/BerriAI/litellm/security)
