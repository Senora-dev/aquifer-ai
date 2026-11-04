# Instructions for AI coding agents

This file tells an AI agent how to work on, test and evaluate Aquifer. Humans: see README.md.

## What this repository is

Aquifer is a self-hosted engineering-context layer served over MCP. Python package in
`src/aquifer/`, production AWS CDK stack in `infrastructure/`, Terraform evaluation environment
in `deploy/terraform/`. It returns neutral facts with provenance — never add scores, risk
assessments or recommendations to responses.

## Setup (no AWS needed)

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"      # add ",cdk" for CDK synth tests
```

## Verify a change

Run these before declaring work done; all run offline without credentials:

```bash
.venv/bin/ruff check src tests scripts infrastructure
.venv/bin/pytest -q                                   # ~200 tests, ~30 s with CDK installed
.venv/bin/python scripts/benchmark_retrieval.py       # must report "failed": 0
```

Try the product locally: `python -m aquifer.local ingest examples/demo/repos`, then
`python -m aquifer.local serve --repos-root examples/demo/repos` (MCP over stdio).

## Evaluate on AWS (only when the user asks)

Use the single entrypoint `deploy/terraform/aquifer-test`; each step prints `==>` headings,
`✓`/`✗` results and machine-readable `KEY=value` lines.

1. `./aquifer-test check` — if it reports expired AWS credentials, ask the user to run
   `aws sso login` (you cannot do it for them).
2. `./aquifer-test seed && ./aquifer-test configure && ./aquifer-test plan` — **show the user the
   resource list and the cost (~$10/day) and get explicit approval before `up`.**
3. `./aquifer-test up`, then `./aquifer-test test` (and `test-updates`).
4. Never run `./aquifer-test down` or `terraform destroy` unless the user asks.

Failure triage: `./aquifer-test status`, `./aquifer-test logs worker 30`, the DLQ count.

## Rules

- Never invent provenance (line numbers, revisions, timestamps): unknown values are `null`.
- No secrets, AWS account ids, customer data or private repository content in code, fixtures,
  logs or docs. Use fictional orgs (`acme/...`) and placeholder ids (`111111111111`).
- Keep tool names and required parameters stable; add only optional fields.
- New behavior needs offline tests (fakes / `InMemoryVectorStore`), and benchmark cases if it
  affects retrieval.
- License is BSL 1.1 (source-available) — do not describe it as open source or change it.
