# Three-minute demo

Everything here is synthetic: `acme/payments`, `acme/platform` and `acme/legacy-billing` are
fictional repositories with fictional revisions and URLs. No AWS account, network access or
credentials are needed.

**Scenario.** An engineer asks their coding agent to change how `billing-api` connects to its
database. Before touching anything, the agent needs to know what depends on `billing-api`, what
`billing-api` depends on, and where those facts are written down.

## 0:00 — Set up (30 s)

```bash
git clone https://github.com/Senora-dev/aquifer-ai.git && cd aquifer-ai
python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
python -m aquifer.local ingest examples/demo/repos
# indexed=17 unchanged=0 failed=0 deleted=0 store=.aquifer-local/index.json
```

Run the ingest again: `indexed=0 unchanged=17` — re-syncing is idempotent and free.

## 0:30 — Connect the agent (30 s)

```bash
claude mcp add aquifer -- "$PWD/.venv/bin/python" -m aquifer.local serve \
  --store "$PWD/.aquifer-local/index.json" --repos-root "$PWD/examples/demo/repos"
```

(Any MCP client works — see [docs/agent-integration.md](../../docs/agent-integration.md). No
agent handy? `python -m aquifer.local serve --repos-root examples/demo/repos --transport http`
and `python examples/mcp/python_client.py billing-api`.)

## 1:00 — Ask (60 s)

> **"What depends on billing-api, and where is that defined?"**

The agent calls `find_related("billing-api")` and gets, among others:

- `orders-service depends_on billing-api` — **explicit**, supported by 2 documents in
  `acme/platform`: `docker-compose.yml` line 6 and `catalog-info.yaml` line 10, each with the
  commit and a line-anchored permalink.
- `billing-api depends_on payments-db` — `docker-compose.yml` line 7 and `catalog-info.yaml`
  line 11 in `acme/payments`; plus `redis` (line 8) and `fx-rates-api` (consumed API).
- `candidates` lists `component:billing-api` and `service:billing-api` separately (two formats,
  not merged), and **nothing** about `billing-api-v2` from `acme/legacy-billing` — a different
  service with a similar name.

> **"Which Terraform stacks use our sqs-queue module v1.4.0?"**

`find_related("git::https://github.com/acme/terraform-modules.git//sqs-queue?ref=v1.4.0")` →
`module.billing_queue` (`acme/payments` `infra/main.tf` line 4) and `module.orders_queue`
(`acme/platform` `terraform/envs/prod/main.tf` line 8). Staging pins `v1.2.0` and is reported
separately.

> **"Why does the ledger use Postgres?"**

`search_context` ranks `docs/adr/0007-ledger-on-postgres.md` first.

## 2:00 — Open the evidence (30 s)

> **"Show me the exact lines."**

`get_document("github:acme/payments:file:docker-compose.yml", line_start=6, line_end=8)` returns
the excerpt `depends_on: - payments-db - redis` together with the path, revision and index time.

## 2:30 — Change the source and re-sync (30 s)

Edit `examples/demo/repos/payments/docker-compose.yml`, replace `redis` with `valkey`, and run
`python -m aquifer.local ingest examples/demo/repos` again (`indexed=1 unchanged=16`). Ask the
first question again: the `redis` edge is gone, `valkey` appears with its new line number.
Delete `docs/adr/0007-ledger-on-postgres.md` and re-sync: it disappears from search and
`get_document` reports `found: false`.

(`git checkout examples/demo` restores the demo data.)

## What the demo does not show

Local mode uses a lexical embedder and no LLM, so prose-only facts — e.g. issue #12's
"checkout-service depends on billing-api" — are not extracted. A deployment (or
`ingest --bedrock`) adds them, labelled `llm_extracted` with `status: claimed`, a verified quote and the line of the
mention.
