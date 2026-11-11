# Security Model

This document states exactly what Aquifer enforces, what it assumes, and what it does not do.
Anything not listed under "enforced" should be treated as not provided.

## Trust boundary: one trust domain per deployment

A deployment ingests a fixed allowlist of repositories (`-c repos=…`). **Every authenticated
MCP client can read every allowlisted repository and nothing else.** Aquifer does not replicate
GitHub's per-user or per-team permissions. Only configure repositories that every token holder
is allowed to read; run separate deployments for separate trust domains.

## Deployment profiles

The controls below apply to every profile unless a row names one. The **small profile**
(`deploy/aws`, one server) differs from the **scale profile** (CDK) in these ways:

| Area | Small profile | Scale profile |
| --- | --- | --- |
| Network exposure | Public subnet with **no inbound ports**. Agents reach MCP through an SSM port forward (IAM-authenticated), or HTTPS on 443 if you set `domain` (Caddy, Let's Encrypt, optional `https_allowed_cidrs`) | Private subnets, internal NLB |
| Egress | Security group allows outbound TCP 443 only, via the instance's Elastic IP; AWS APIs are reached over their public endpoints (TLS, SigV4) | Outbound 443 only, via NAT; AWS APIs through VPC endpoints |
| Secrets | SSM Parameter Store SecureStrings under `/aquifer/<name>/` (AWS-managed key). The GitHub token is copied from `gh` through a 0600 temporary file, never argv; the MCP token is generated on the server. Neither enters Terraform state | Secrets Manager |
| Instance | IMDSv2 required (hop limit 1), no SSH key, no inbound rules. Service runs as a non-root system user with `ProtectSystem=strict`, `NoNewPrivileges`, `PrivateTmp`, and write access only to its data and log directories | Fargate, non-root, read-only root filesystem |
| IAM | Bedrock invoke on the configured model ARNs only; SSM parameters under the deployment's own path; read on its own artifact bucket; write to its own log group; SSM managed-instance core | Per-role least privilege |
| Data at rest | Encrypted EBS root and data volumes; snapshots inherit encryption | OpenSearch Serverless (AWS-owned key), SQS SSE, S3 SSE |

## MCP Integrator

Content imported from external MCP servers enters the same trust domain: every Aquifer token
can read it. The Integrator:
- calls only approved read-only tools (annotation *and* name checks)
- refuses private network destinations (including OAuth endpoints a server advertises)
- treats remote tool descriptions and results as untrusted data
- stores credentials as SSM SecureStrings or 0600 files

Details: [integrator.md](./integrator.md#safety). Tests: `tests/test_integrator.py`.

## Enforced controls

| Control | How it is enforced | Tested in |
| --- | --- | --- |
| No unauthenticated MCP access | Every HTTP request needs `Authorization: Bearer <token>` (constant-time compare, tokens ≥ 32 chars). The server refuses to start without tokens; unauthenticated mode is only accepted on a loopback bind. | `tests/test_mcp_server.py` (real HTTP + MCP client), CI image check |
| No public endpoint | Internal NLB with its own security group admitting only the VPC CIDR and explicitly configured private CIDRs (`0.0.0.0/0` rejected at synth); tasks accept traffic only from the NLB | `tests/test_infrastructure.py` |
| Consistent authorization | All five tools run `authorize → before_query → repository scope → store → after_query → audit`. (Before this release three tools skipped the query hooks.) | `tests/test_mcp_tools.py`, `tests/test_mcp_reasoning_tools.py` |
| No cross-source leaks | Queries are always filtered to the allowlist; results outside it are dropped again after retrieval; out-of-scope documents are indistinguishable from missing ones. De-listed repositories are purged from the index by discovery. | same, `tests/test_discovery.py` |
| Input validation | Filter fields restricted to an allowlist, values must be scalars (no query-DSL passthrough), `k` bounded | `tests/test_mcp_tools.py`, `tests/test_opensearch.py` |
| Secrets handling | GitHub and MCP tokens in Secrets Manager; MCP tokens injected as an ECS secret (not plaintext in the task definition); stack outputs contain ARNs only; tokens never logged; `auth_tokens` hidden from settings repr | `tests/test_infrastructure.py`, `tests/test_mcp_server.py` |
| Least-privilege IAM | Bedrock invoke limited to the configured model ARNs; MCP role may only embed and has a read-only OpenSearch data policy; ingestion roles have write access; no `aoss:*` | `tests/test_infrastructure.py` |
| Network egress | Application security groups allow outbound TCP 443 only; Bedrock, Secrets Manager, SQS, OpenSearch via VPC endpoints; S3 via gateway endpoint | `tests/test_infrastructure.py` |
| Container hardening | Non-root user, read-only root filesystem | `tests/test_infrastructure.py`, CI image check |
| Audit | `mcp_tool_call` (tool, principal, outcome, counts, latency) and `mcp_auth_failed` events — no query text or content | `tests/test_mcp_server.py` |
| UI access (single-server profile) | Sign-in with an MCP token; HttpOnly SameSite=Strict signed session, ended when the token is revoked or rotated; reads use the same tool path and scope; changing sources or tokens needs a principal in `AQUIFER_WEB__ADMIN_PRINCIPALS`; mutating calls need an `X-Aquifer-CSRF` header; strict CSP, `X-Frame-Options: DENY`; `ui_*` audit events (no question text) | `tests/test_web.py` |
| Encryption | OpenSearch Serverless with AWS-owned key; SQS SSE; S3 SSE + TLS-only; Secrets Manager | `tests/test_infrastructure.py` |

## Agent credentials

Each agent should use its own credential, created on the UI's Agents page. Agent credentials
reach only the MCP tools, which are read-only; they are refused at UI sign-in and so cannot use
admin actions (sources, tokens). They are stored with the other tokens (SSM SecureString on
AWS), shown once at creation, never inlined in the setup snippets (which read
`AQUIFER_TOKEN` from the environment), and revocable with immediate effect. Missing, invalid
and revoked credentials get `401` from `/mcp`. There is no OAuth: clients that only support
OAuth for remote MCP servers cannot connect.

On AWS, Terraform state holds no secrets (tokens live in SSM under `/aquifer/<name>/`); with
`--backend s3` it is encrypted at rest and locked.

## Known limitations (not provided)

- **Per-user / per-agent source permissions.** All tokens share one trust domain.
- **SSO / OIDC / RBAC.** Tokens are static bearer secrets. Rotate by editing the secret and
  restarting the MCP service.
- **Transport encryption inside the VPC by default (scale profile).** Without
  `-c mcp_certificate_arn`, the NLB listener is plain TCP on port 80 and bearer tokens cross the
  VPC network unencrypted. Use a certificate for anything beyond a tightly controlled VPC. In the
  small profile, traffic is encrypted by the SSM tunnel or by HTTPS when `domain` is set.
- **High availability (small profile).** One instance in one AZ; see docs/cost.md#tradeoffs.
- **Customer-managed KMS keys.** The collection uses an AWS-owned key.
- **Egress allowlisting by domain.** Outbound HTTPS is allowed to any destination (through the
  NAT gateway or the small profile's Elastic IP). A network firewall or proxy is needed to
  restrict egress to `api.github.com` and AWS endpoints.
- **Deleted issues/PRs** are not detected (see README limitations).

The interceptor seam (`authorize`, `before_query`, `after_query`, `before_ingest`) receives the
authenticated principal and is the supported place to add per-principal rules, redaction or an
external policy engine without modifying the core.

## Data residency

Indexed content, embeddings and metadata stay in the deployment account and region. Bedrock is
called in the deployment's region (through a VPC endpoint in the scale profile); a cross-region *inference profile*
configured as the extraction model may route requests to other regions in its geography.
GitHub content is fetched over the internet from `api.github.com`.

## Prompt injection and untrusted content

Repository content is untrusted input — an issue body can contain text written to manipulate an
AI agent.

- **At extraction:** document content is wrapped in `<artifact_content>` delimiters (which the
  content cannot close), and the system prompt instructs the model to treat it as data and never
  follow instructions inside it. The model's output is parsed through a schema without
  provenance fields, so it cannot label its own output as an explicit reference; every
  LLM-extracted relationship is labelled `llm_extracted` and checked against the text
  (`quote_verified`, `mention_found`), and gets a `status` derived from the document context —
  model output can never become `current`.
- **At retrieval:** the MCP server instructions, tool descriptions and a `notice` field on
  document and relationship responses tell the calling agent that text, summaries and snippets
  are source data, not instructions. Aquifer never executes or follows retrieved content.
- **Residual risk:** a calling agent can still be influenced by malicious text it reads. Agents
  that act on retrieved context should apply their own policies (e.g. require human approval for
  destructive actions).

## Reporting vulnerabilities

See [SECURITY.md](../SECURITY.md).
