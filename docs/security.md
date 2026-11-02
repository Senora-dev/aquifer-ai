# Security Model

This document states exactly what Aquifer enforces, what it assumes, and what it does not do.
Anything not listed under "enforced" should be treated as not provided.

## Trust boundary: one trust domain per deployment

A deployment ingests a fixed allowlist of repositories (`-c repos=…`). **Every authenticated
MCP client can read every allowlisted repository and nothing else.** Aquifer does not replicate
GitHub's per-user or per-team permissions. Only configure repositories that every token holder
is allowed to read; run separate deployments for separate trust domains.

## Enforced controls

| Control | How it is enforced | Tested in |
| --- | --- | --- |
| No unauthenticated MCP access | Every HTTP request needs `Authorization: Bearer <token>` (constant-time compare, tokens ≥ 32 chars). The server refuses to start without tokens; unauthenticated mode is only accepted on a loopback bind. | `tests/test_mcp_server.py` (real HTTP + MCP client), CI image check |
| No public endpoint | Internal NLB; MCP security group admits only the VPC CIDR or explicitly configured private CIDRs (`0.0.0.0/0` rejected at synth) | `tests/test_infrastructure.py` |
| Consistent authorization | All five tools run `authorize → before_query → repository scope → store → after_query → audit`. (Before this release three tools skipped the query hooks.) | `tests/test_mcp_tools.py`, `tests/test_mcp_reasoning_tools.py` |
| No cross-source leaks | Queries are always filtered to the allowlist; results outside it are dropped again after retrieval; out-of-scope documents are indistinguishable from missing ones. De-listed repositories are purged from the index by discovery. | same, `tests/test_discovery.py` |
| Input validation | Filter fields restricted to an allowlist, values must be scalars (no query-DSL passthrough), `k` bounded | `tests/test_mcp_tools.py`, `tests/test_opensearch.py` |
| Secrets handling | GitHub and MCP tokens in Secrets Manager; MCP tokens injected as an ECS secret (not plaintext in the task definition); stack outputs contain ARNs only; tokens never logged; `auth_tokens` hidden from settings repr | `tests/test_infrastructure.py`, `tests/test_mcp_server.py` |
| Least-privilege IAM | Bedrock invoke limited to the configured model ARNs; MCP role may only embed and has a read-only OpenSearch data policy; ingestion roles have write access; no `aoss:*` | `tests/test_infrastructure.py` |
| Network egress | Application security groups allow outbound TCP 443 only; Bedrock, Secrets Manager, SQS, OpenSearch via VPC endpoints; S3 via gateway endpoint | `tests/test_infrastructure.py` |
| Container hardening | Non-root user, read-only root filesystem | `tests/test_infrastructure.py`, CI image check |
| Audit | `mcp_tool_call` (tool, principal, outcome, counts, latency) and `mcp_auth_failed` events — no query text or content | `tests/test_mcp_server.py` |
| Encryption | OpenSearch Serverless with AWS-owned key; SQS SSE; S3 SSE + TLS-only; Secrets Manager | `tests/test_infrastructure.py` |

## Known limitations (not provided)

- **Per-user / per-agent source permissions.** All tokens share one trust domain.
- **SSO / OIDC / RBAC.** Tokens are static bearer secrets. Rotate by editing the secret and
  restarting the MCP service.
- **Transport encryption inside the VPC by default.** Without `-c mcp_certificate_arn`, the NLB
  listener is plain TCP on port 80 and bearer tokens cross the VPC network unencrypted. Use a
  certificate for anything beyond a tightly controlled VPC.
- **Customer-managed KMS keys.** The collection uses an AWS-owned key.
- **Egress allowlisting by domain.** The NAT gateway permits HTTPS to any destination; a
  network firewall or proxy is needed to restrict egress to `api.github.com`.
- **Deleted issues/PRs** are not detected (see README limitations).

The interceptor seam (`authorize`, `before_query`, `after_query`, `before_ingest`) receives the
authenticated principal and is the supported place to add per-principal rules, redaction or an
external policy engine without modifying the core.

## Data residency

Indexed content, embeddings and metadata stay in the deployment account and region. Bedrock is
called in the stack's region through a VPC endpoint; a cross-region *inference profile*
configured as the extraction model may route requests to other regions in its geography.
GitHub content is fetched over the internet (through the NAT gateway) from `api.github.com`.

## Prompt injection and untrusted content

Repository content is untrusted input — an issue body can contain text written to manipulate an
AI agent.

- **At extraction:** document content is wrapped in `<artifact_content>` delimiters (which the
  content cannot close), and the system prompt instructs the model to treat it as data and never
  follow instructions inside it. The model's output is parsed through a schema without
  provenance fields, so it cannot label its own output as an explicit reference; every
  LLM-extracted relationship is labelled `llm_extracted` and checked against the text
  (`grounded`).
- **At retrieval:** the MCP server instructions, tool descriptions and a `notice` field on
  document and relationship responses tell the calling agent that text, summaries and snippets
  are source data, not instructions. Aquifer never executes or follows retrieved content.
- **Residual risk:** a calling agent can still be influenced by malicious text it reads. Agents
  that act on retrieved context should apply their own policies (e.g. require human approval for
  destructive actions).

## Reporting vulnerabilities

See [SECURITY.md](../SECURITY.md).
