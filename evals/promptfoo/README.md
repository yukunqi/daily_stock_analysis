# Promptfoo evals

This suite exercises the user-facing Agent chat API path:

```bash
npm run evals
```

By default the Promptfoo provider uses a deterministic fixture executor through
`POST /api/v1/agent/chat`, so it needs no API keys, customer data, local server,
or networked market-data access. To run against the live configured Agent stack,
set `DSA_PROMPTFOO_MODE=live` and provide the existing Agent/LLM environment
variables required by the app, such as `AGENT_MODE=true` and `LITELLM_MODEL` or
`AGENT_LITELLM_MODEL` plus the matching provider credentials.

The seed cases cover support-answer quality, tool-call planning, retrieval
grounding, business-rule safety, and agent task completion shape.

Promptfoo is pinned as a local dev dependency so the suite does not depend on a
fresh `npx` install every run.
