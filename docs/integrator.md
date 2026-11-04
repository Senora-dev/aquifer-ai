# MCP Integrator

The MCP Integrator is part of the Aquifer server. It connects to the MCP servers your tools
already offer, pulls their content on a schedule, and puts it through the same pipeline as
GitHub: extraction, relationship status, embeddings, provenance and freshness. Agents then find
it through Aquifer's MCP tools alongside everything else.

There is no separate agent to run, no ingestion script to write, and nothing to forward by hand.
It runs inside `aquifer serve`, so it adds no infrastructure and no baseline cost (see
[cost.md](./cost.md)).

```
GitHub / Atlassian / Linear / Notion / any MCP server
        │  approved read-only tools only, on a schedule (default daily)
        ▼
aquifer serve ── MCP Integrator ──▶ shared ingestion pipeline ──▶ index
                                    (extraction, status, embeddings, provenance)
        ▲
agents ─┘  search_context / find_related / get_document / list_sources
```

## What "connecting a tool" does, and does not, mean

- **Pull, on a schedule, with checkpoints.** Each source runs a *sync plan*: for every stream,
  one approved read-only tool lists items page by page, and an optional detail tool fetches the
  full content. Runs are deterministic: no model decides what to call during a sync.
- **Synchronization coverage depends on the tool.** Being able to *read* from an MCP server
  does not mean every change is detected. Each stream states its coverage (shown at preview, in
  `aquifer sources status` and in `list_sources`):
  - **Changes.** Incremental when the listing is ordered by update time. Otherwise every run
    re-lists everything and compares update times and content. When the source exposes no
    update time, edits are found only by comparing content.
  - **Deletions.** Inferred only from a **complete** listing that reached its end. Each missing
    item is confirmed with the source's detail or verify tool when the plan has one. A listing
    that suddenly returns nothing, or would remove most of a stream, is not trusted when there
    is no per-item check. Search and filtered listings (`partial`) never delete anything:
    absence from search results is not evidence of deletion.
- **No writes.** Only tools on the source's approved list are called, and only read-only tools
  can be approved (see [Safety](#safety)).

## Onboarding (copy-paste walkthrough)

On the AWS small profile, run the commands through the deploy script; it executes them on the
server over SSM. In Docker, use `docker compose exec aquifer aquifer sources …`. On a host, use
`aquifer sources …`.

### Example: GitHub discussions and release notes

This uses GitHub's own MCP server, and was verified end to end. The native GitHub connector
already covers issues, PRs and files; this adds discussions and release notes.

```bash
cd deploy/aws
# 1. Choose the tool. (`./aquifer-aws sources providers` lists the known ones.)
# 2. Authorize: a read-only token goes from your laptop straight into SSM.
./aquifer-aws source-token eng-github --from-gh           # or: … source-token eng-github < token.txt
./aquifer-aws sources add eng-github --provider github --token-ref ssm:/aquifer/aquifer/sources/eng-github/token
./aquifer-aws sources tools eng-github                     # what the server offers, and what is allowed
# 3. Select content
./aquifer-aws sources template eng-github --stream discussions --target acme/payments
./aquifer-aws sources template eng-github --stream releases --target acme/payments
# 4. Preview sample documents and the sync coverage (indexes nothing)
./aquifer-aws sources preview eng-github
# 5. First import now, then daily
./aquifer-aws sources enable eng-github
# 6. Health, progress, last success, actionable errors
./aquifer-aws sources status
./aquifer-aws sources status eng-github                    # full JSON for one source
```

Then ask your agent, for example: *"What does the payments-db failover runbook say?"* The
answer comes from `search_context`, with a link to the discussion and the server and tool it was
imported through.

### Example: an OAuth provider (Atlassian, Linear, Notion, Sentry, …)

```bash
./aquifer-aws sources add jira --provider atlassian
./aquifer-aws authorize jira              # opens the SSM tunnel and prints a URL; approve in the browser
./aquifer-aws sources tools jira
./aquifer-aws sources propose jira --stream eng-tickets --tool <a list/search tool> \
    --args '{"…": "…"}' --kind ticket [--complete]
./aquifer-aws sources preview jira && ./aquifer-aws sources enable jira
```

`propose` calls the list tool once and derives a stream from the result. It recognizes the
item list, ids, titles, bodies, URLs, update times and cursor or page pagination. Check the
proposed mapping in the output and adjust it if needed:

```bash
./aquifer-aws sources plan jira                       # current plan and coverage
./aquifer-aws run sources plan jira --export p.json   # on a host: edit locally, then --import
```

Mark a stream `--complete` only when its tool lists **every** item of its container (for
example, all pages of a space). That is what allows deletions to be inferred.

### Any other MCP server

```bash
aquifer sources add internal-wiki --url https://mcp.wiki.example.com/mcp --auth bearer --token-stdin < token
aquifer sources add docs --url https://mcp.example.com/mcp --auth oauth
aquifer sources add slack --provider slack --client-id <your Slack app's client id> --token-stdin < client-secret
```

## Providers

| Provider | URL | Auth | Status |
| --- | --- | --- | --- |
| **GitHub** (remote MCP server) | `https://api.githubcopilot.com/mcp/` | Personal access token (read-only) | **Verified end to end (2026-11-04)**, locally and on AWS (18/18). Token auth, server-side read-only mode (`X-MCP-Readonly`), 28 read-only tools, cursor and page pagination, not-found errors; discussions and release notes imported, re-run unchanged, edited, retried after a network fault, recovered after a crash, deleted. Templates: `discussions`, `releases` |
| Atlassian Rovo (Jira, Confluence) | `https://mcp.atlassian.com/v1/mcp` | OAuth 2.1 (dynamic registration, PKCE, refresh) | Auth metadata verified; tools not verified (needs an Atlassian Cloud site). Your org admin may need to allow it |
| Linear | `https://mcp.linear.app/mcp` | OAuth 2.1 (scope `read` requested; Aquifer keeps it even though the server advertises `read write`) | Auth metadata and dynamic registration with a loopback redirect verified; login and tools pending |
| Notion | `https://mcp.notion.com/mcp` | OAuth 2.1 (no read-only scope offered) | Auth metadata and dynamic registration with a loopback redirect verified; tools not verified |
| Sentry | `https://mcp.sentry.dev/mcp` | OAuth 2.1 | Auth metadata verified; tools not verified |
| Slack | `https://mcp.slack.com/mcp` | OAuth without dynamic registration: bring your own Slack app | Auth metadata verified; tools not verified. Check Slack's terms on storing message data |

"Auth metadata verified" means the server's published OAuth metadata (RFC 9728 / RFC 8414) was
checked for what Aquifer needs. Those providers work through the generic path (`propose`).
Their tool names and output shapes have not been tested with a real account, so no templates
are shipped for them yet.

## Authorization boundaries: who can read imported content

- What a source imports is limited by the account that authorized it: its token or OAuth grant.
- What it imports is readable by **every MCP token of this Aquifer deployment** (one trust
  domain), regardless of permissions in the original tool. Connect only content that everyone
  with an Aquifer token may read; use separate deployments for separate audiences. Preview and
  status state this explicitly.
- A paused or removed source's documents are not returned by any tool. `remove` also deletes
  them from the index, along with the stored credentials.

## Safety

- **Read-only gate.** A tool can be approved only if the server annotates it read-only *and* its
  name does not read like a write (`create`, `update`, `delete`, `send`, `run`, …). A tool named
  `delete_page` is refused even if the server claims it is read-only. Tools without annotations
  need `--allow-unannotated` on the source, and even then only read-like names
  (`get`/`list`/`search`/…) are accepted.
- **Approved list enforced at call time.** Any other call is refused. Each run checks the plan
  against the tools the server offers *now*: a tool that disappeared or changed class stops the
  run with an explanation.
- **Untrusted remote input.** Tool names, descriptions and results are data. Descriptions are
  shown to the operator sanitized; nothing remote is executed or followed as an instruction. No
  LLM chooses tools during a sync. Imported text is treated like any other source text.
- **Network guard.** Source URLs and every OAuth endpoint a server advertises must be public
  HTTPS addresses. Private, loopback and link-local addresses (including the instance metadata
  service) are refused unless `AQUIFER_INTEGRATOR__ALLOW_PRIVATE_NETWORKS=true`.
- **Bounded work.** Tool results over 5 MB are rejected. Each stream has per-run item and page
  limits. Pages are spaced to respect the source's rate limits.
- **Credentials.** Stored as SSM SecureStrings (AWS) or 0600 files, never in configuration,
  Terraform state, logs or command lines.
- **OAuth.** Authorization code with PKCE, RFC 8707 resource indicators and issuer checks, via
  the MCP SDK.
  - The redirect returns to `<public_url>/oauth/callback`. On AWS that is the SSM tunnel, or
    your domain.
  - The route accepts only a pending, unexpired, single-use `state`.
  - Refresh tokens are renewed unattended; a token that stops working moves the source to
    `needs_reauth` with the command to fix it.

## Reliability

Every page is a job on the server's durable queue:
- **Interrupted runs** resume from the last page.
- **Duplicate deliveries** and re-runs are idempotent: unchanged content is skipped by hash, and
  items whose update time is unchanged are not fetched again.
- **Rate limits** postpone the page.
- **Transient errors** retry with backoff and end in the dead letters if they persist.

| State | Meaning | What to do |
| --- | --- | --- |
| `needs_authorization` / `needs_reauth` | No working credentials | `aquifer sources authorize NAME` (bearer: store a new token, re-add with `--replace`) |
| `error` | Last run failed (e.g. `plan_invalid`: the server's tools changed) | `aquifer sources status NAME` shows the reason and the next command |
| `active` | Syncing on schedule | — |
| `paused` | No syncs; documents hidden | `aquifer sources enable NAME` |

## What has been verified, and how

| Behavior | Real services (GitHub's MCP server, AWS) | Offline tests (local MCP + OAuth server) |
| --- | --- | --- |
| Initial import, paged (1 item per page) | ✅ AWS, 2026-11-04 | ✅ |
| Retrieval through Aquifer's MCP tools, provenance, `list_sources` coverage | ✅ AWS | ✅ |
| Unchanged content: a re-run indexes nothing and makes **no** detail calls | ✅ AWS | ✅ |
| Update: only the changed item is fetched (1 detail call) and re-indexed | ✅ AWS | ✅ |
| Retries: run during a network fault (all non-AWS HTTPS cut) fails as `source_unavailable`, is retried by the queue, and succeeds once the fault ends | ✅ AWS | ✅ (rate-limit postponement) |
| Crash recovery: `kill -9` while a page job runs; on restart, 2 interrupted jobs were recovered; the run completed; 0 duplicate generations | ✅ AWS | ✅ |
| Deletions: a discussion and a release deleted at the source, each confirmed by GitHub (`get_discussion`, `get_release_by_tag`) and removed; the others kept | ✅ AWS | ✅ (also: kept when only missing from the listing; never on partial or empty listings) |
| Bearer token auth; rejected token → `needs_reauth` | ✅ GitHub PAT (accepted) | ✅ (rejection) |
| Read-only gate, approved tools only, private-network guard | Classification checked on GitHub's 28 tools | ✅ |
| OAuth 2.1: registration, PKCE, authorize, unattended refresh after restart, forced refresh, re-auth, least-privilege scope | Registration with Linear and Notion (loopback redirect accepted); full login pending a user click | ✅ |
| Credentials > 4 KB in SSM (advanced tier) | ✅ AWS SSM | — |

The AWS checks ran on an isolated deployment (`aquifer-val`, destroyed afterwards). Command:
`./aquifer-aws test-integrator`. All source mutations went to a dedicated fixture repository
(see [Test fixtures](#test-fixtures)).

## Test fixtures

`./aquifer-aws test-integrator` mutates only the fixture repository set by
`AQUIFER_INTEGRATOR_FIXTURE`:
- default: `<owner>/aquifer-test-integrator`, created by `./aquifer-aws seed`;
- otherwise: `<owner>/aquifer-test-legacy-billing`, which no other test mutates.

Each run creates discussions and a release named with a random marker (`E2E <marker> …`,
`e2e-<marker>`) and deletes them at the end, even if a check fails. The fixture is left with
Discussions enabled and no discussions or releases.

## Scale profile

The CDK scale profile (Lambda + SQS ingestion, OpenSearch Serverless, Fargate MCP) does **not**
run the Integrator. This is a deliberate scope limit of this release, not an oversight. The
Integrator needs:
- durable local state between runs: the source registry, per-stream checkpoints, ids seen
  during a run, and pending authorizations. The scale profile's compute is stateless (Lambda;
  Fargate with a read-only root filesystem and no volume).
- a long-running process to finish the interactive OAuth step and receive the browser
  redirect. The scale profile's only HTTP entry point is an internal load balancer that a
  user's browser normally cannot reach.
- writable credential storage for rotating OAuth refresh tokens. The scale profile's tasks
  only read Secrets Manager.

Supporting it means moving that state to DynamoDB or S3, running the page jobs on SQS and
Lambda, writing renewed tokens to Secrets Manager, and exposing an authenticated callback. That
is a separate project. Until then, organizations that need the Integrator use the small profile
or the container. Running a single Integrator node against the scale profile's OpenSearch index
should work, because the pipeline is store-agnostic, but it is untested and not supported.

## Cost

- **Infrastructure:** none beyond the server; the Integrator runs in the same process.
- **Model usage:** the same as any document, about $0.0044 per new or changed document with
  Claude Haiku 4.5 extraction (see [cost.md](./cost.md)).
- **Provider fees:** none for GitHub's MCP server (it uses your token's normal API limits).
  Other providers' MCP servers are part of their own plans; check their terms and rate limits.

## Limitations

- The Integrator runs in the single-server profile (`aquifer serve`, the AWS small profile and
  Docker). The CDK scale profile does not run it (see [Scale profile](#scale-profile)).
- Only GitHub has been verified end to end. Other providers use the generic path, and their
  stream mappings need checking at preview.
- Changes are detected only as well as the source's tools allow. Each stream's coverage is
  stated; there is no guarantee of real-time freshness (the default is daily).
- Comments, attachments and permissions inside the external tool are not imported unless a
  stream maps them.
- OAuth authorization needs a browser that reaches the server's callback: the SSM tunnel on the
  same computer, or a configured domain.
