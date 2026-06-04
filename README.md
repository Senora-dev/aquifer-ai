<h1 align="center">Aquifer AI</h1>

<p align="center">
  <img src="https://raw.githubusercontent.com/Senora-dev/aquifer-ai-assets/main/logo.png" alt="Aquifer AI logo" width="90">
</p>

An open-source, **in-VPC Context Lake** — neutral **infrastructure for AI agents**, not a
developer portal and not a reasoning engine. Aquifer is headless: it aggregates engineering
context (GitHub first) into an OpenSearch vector store and exposes a standard **MCP API** that
AI agents query directly. All inside your own VPC, deployable as a single CDK stack.

> **Agents are the users; Aquifer stays neutral.** No UI, ever. And no verdicts: Aquifer provides
> objective, organized context (entities, relationships, keys) and lets the **agent** do the
> reasoning. We aim to be the best **Search & Retrieval** layer in the industry — not a
> decision-maker.

## Why

AI agents are only as good as the context they can reach. Aquifer is a single, reliable
**Search & Retrieval** layer over your engineering sources: it ingests, embeds, and—at ingestion
time—extracts **neutral, objective metadata** (typed entities like `service:billing-api` and
`jira_key:PROJ-400`, plus factual relationships such as `depends_on`/`references`/`part_of`) and
indexes it as queryable fields. An agent can then retrieve exactly the context it needs to reason
on its own — e.g. gather everything related to a service before deciding whether to deploy — with
zero data leaving your network and **no external AI processing** beyond your own Bedrock.

Aquifer draws **no conclusions and makes no value judgments**. It does not score, rank by risk,
or answer "can I deploy?" — it returns the facts; the agent decides.

> **Status:** a functional, neutral Context Lake. It ingests, embeds, and serves context over
> MCP; **semantic indexing** (in the `before_ingest` hook) extracts objective entities and
> relationships via a modular per-source prompt registry and indexes them as queryable fields;
> and the MCP tools let agents search and traverse that metadata.

## MCP tools

Agents reach the lake through five neutral retrieval tools (no verdicts, no interpretation):

- `search_context(query, k?, filters?)` — semantic k-NN search with metadata filters.
- `get_document(document_id)` — a full document, chunks reassembled.
- `list_sources()` — the configured sources.
- `find_related(entity, relationship_types?, k?)` — **retrieve relationships**: items whose
  extracted relationships point at an entity, plus items that reference it, merged and annotated
  by how they matched.
- `list_entities(document_id?, entity_type?)` — a neutral **inventory** of the distinct
  `{type, name}` entities extracted (e.g. services, datastores, Jira keys).

## Architecture (baseline)

- **Vector store:** Amazon OpenSearch Serverless (k-NN vectors)
- **Compute:** Lambda for ingestion (SQS fan-out), Fargate for the persistent MCP server
- **Embeddings:** Amazon Bedrock via a VPC (PrivateLink) endpoint
- **Connectors:** GitHub (issues, PRs, READMEs, discussions); more are additive via the
  `Connector` interface

<p align="center">
  <img src="https://raw.githubusercontent.com/Senora-dev/aquifer-ai-assets/main/architecture.png" alt="Aquifer AI architecture" width="800">
</p>

## Documentation

Full documentation lives in [`docs/`](./docs):

- [Architecture](./docs/architecture.md) — the single-stack CDK design and pipeline.
- [Concepts](./docs/concepts.md) — Context Lake, Semantic Indexing, the Neutrality Principle, MCP.
- [Getting Started](./docs/getting-started.md) — deploy the stack into your AWS account.
- [Contributing](./docs/contributing.md) — add a `SourceType`/connector or an interceptor.

## Layout

```
src/aquifer/        # Python package: core, connectors, adapters, ingestion Lambdas, MCP server
infrastructure/     # AWS CDK (Python) — the single deployable stack
scripts/            # dev tooling (e.g. the semantic-indexing eval harness)
tests/              # unit + fixture tests (incl. tests/eval_data/ golden artifacts)
```

## Tuning semantic indexing

Extraction quality is governed by the modular prompts in `aquifer/semantic/prompts.py`. To tune
them, run the eval harness over the golden artifacts in `tests/eval_data/` — it prints the
selected prompt, the generated neutral metadata JSON, and recall against the expected objective
fields (entity names, relationship targets):

```bash
python scripts/eval_semantic_index.py            # all examples (calls your own Bedrock)
python scripts/eval_semantic_index.py --name jira --verbose
```

The scoring is intentionally neutral — it measures whether we extracted the right *facts*, not
any judgment. Add more `*.json` examples to `tests/eval_data/` to grow coverage.

## Modularity & the enterprise seam

The core is intentionally lean. Cross-cutting concerns (SSO, RBAC, audit logs) are **not** in
this baseline — instead, core exposes an `Interceptor` seam (`before/after_ingest`,
`before/after_query`, `authorize`) with pass-through no-op implementations. Enterprise features
ship as a separate package that registers interceptors; **core never imports enterprise code**.

## Quickstart

Deploy the whole Context Lake into your own AWS account as a single CDK stack. The happy path is
below; for configuration keys, ingestion details, and troubleshooting see
[Getting Started](./docs/getting-started.md).

**Prerequisites**

- **Docker running** — CDK bundles the Lambda code and builds the MCP image locally at deploy time.
- **AWS CLI v2**, configured with credentials (`aws configure`).
- **Amazon Bedrock model access** enabled in your target region for the embedding model
  (Titan Text Embeddings v2) and an inference (Claude) model.

**1. Install and bootstrap**

```bash
pip install -e ".[dev,cdk]"        # package + tooling + CDK libraries
npm install -g aws-cdk             # CDK Toolkit CLI

export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export CDK_DEFAULT_REGION=us-east-1     # your target region
cd infrastructure
cdk bootstrap                      # one-time per account/region
```

**2. Deploy the stack**

```bash
cdk deploy -c repos='["your-org/your-repo"]'
```

On completion, CloudFormation prints the stack outputs, including `McpEndpoint` (the internal MCP
API) and `GitHubTokenSecretArn` (the secret to populate next).

**3. Inject the GitHub token**

Store a fine-grained PAT with read access to the target repos in the created secret:

```bash
aws secretsmanager put-secret-value \
  --secret-id <GitHubTokenSecretArn> \
  --secret-string 'ghp_xxxxxxxxxxxxxxxxxxxx'
```

Discovery runs on a schedule (every 15 minutes by default); ingestion, semantic indexing, and
embedding then proceed automatically.

### Security & Architecture Note

By design, Aquifer deploys into an **isolated VPC and exposes no public endpoints** — nothing is
reachable from the internet, and no data leaves your network. The MCP API is served on an
**internal** load balancer (`McpEndpoint`), so calling it from `localhost` will not work.

Reach the MCP API from inside the network boundary — for example from an **EC2 bastion** or
**Cloud9** environment in the VPC, or from your own machine over **Client VPN** or **VPC peering**.
Point an MCP-capable agent at the `McpEndpoint` over HTTP/SSE and it can call `search_context`,
`find_related`, `list_entities`, and the other tools.

## License

**Business Source License 1.1 (BSL 1.1)** — see the [`LICENSE`](LICENSE) file.
Converts to Apache 2.0 on the Change Date.
