# The Aquifer UI

`aquifer serve` also serves a browser UI at `/`, on the same port as the MCP endpoint (`/mcp`).
It covers the first session end to end: connect GitHub, follow indexing, ask a question, open the
exact source behind the answer, and connect an agent. It has no build step and loads nothing from
the internet (plain HTML, CSS and JavaScript in `src/aquifer/web/static/`).

## Open it

| Where Aquifer runs | UI address |
| --- | --- |
| Laptop, no AWS | `python -m aquifer.local ui`, then <http://127.0.0.1:8080/> |
| AWS small profile | `aquifer install aws` opens it at the end; later `deploy/aws/aquifer-aws connect` reopens the tunnel; then <http://127.0.0.1:18080/> ([install-aws.md](./install-aws.md)) |
| Docker | <http://localhost:8080/> (put TLS in front before exposing it beyond localhost) |

`python -m aquifer.local ui` keeps everything under `.aquifer-local/server` (SQLite index, queue,
settings) and uses the local lexical embedder, so it needs no credentials. Add `--auth token` to
practice the sign-in (a token is created in `DATA/mcp-tokens.json`), or `--bedrock` to use Bedrock
embeddings, extraction and answers.

Set `AQUIFER_WEB__ENABLED=false` to serve MCP only. The scale profile (CDK) runs the MCP server
alone and has no UI.

## Who can do what

The UI uses Aquifer's existing access model and enforces it on the server:

- **Sign in** with an Aquifer access token (on AWS, the admin token: `deploy/aws/aquifer-aws
  token | pbcopy`). Agent credentials (created on the Agents page) are MCP-only and are refused
  here (`403`). The session is an HttpOnly, SameSite=Strict cookie signed with a key in the data
  directory. Revoking or rotating the token ends its sessions.
- **Reading** (asking, opening sources, seeing status) is open to every valid token, exactly like
  MCP: one trust domain. Reads go through the same protected path as the MCP tools
  (interceptors, repository scope, audit events).
- **Changing** sources and access tokens needs an admin principal, listed in
  `AQUIFER_WEB__ADMIN_PRINCIPALS` (default `["default"]`, the token `aquifer init` and the
  deploy script create). Others see a read-access state, and the API answers 403.
- Mutating requests also need an `X-Aquifer-CSRF` header. Pages are served with a strict
  Content-Security-Policy (`script-src 'self'`, no inline code).
- With `AQUIFER_MCP__AUTH_MODE=none` (allowed on loopback only), the person at the machine is
  `local` with admin rights, as for the stdio server.

## Connecting GitHub

The GitHub flow uses the native connector (READMEs, issues, PRs and decision files) and the
existing discovery and worker path:

1. **Token.** A fine-grained token with read-only Contents, Issues, Pull requests and Metadata.
   Aquifer checks it with GitHub, lists what it can read, and reports classic-token write scopes.
   If the deployment already configures `AQUIFER_GITHUB__TOKEN_SECRET`, that token is used and
   none is asked for.
2. **Repositories.** Only repositories the token can read can be chosen (checked again on the
   server).
3. **Preview.** What indexing would read from each chosen repository, from read-only GitHub calls
   with the same rules as indexing: the README, files matching `github.file_globs` (ADRs,
   Terraform, Compose, Backstage) with samples, and counts and samples of issues and pull
   requests (from the first page, so "at least" when it is full). Nothing is stored yet.
4. **Review access.** What will be read, that nothing is written, where the token is stored, and
   who will be able to read the content. Enabling indexing needs an explicit acknowledgement.
5. **Indexing.** Progress per repository; the page offers the first question as soon as one is
   searchable.

Repositories chosen in the UI are stored in `<data_dir>/github-source.json` and added to
`AQUIFER_GITHUB__REPO_ALLOWLIST`, never replacing it. The token is stored as a secret reference
under `AQUIFER_INTEGRATOR__CREDENTIALS_PREFIX` (SSM on AWS) or in `<data_dir>/credentials/`
(mode 0600). `aquifer status` and the other CLI commands see the same repositories.

**GitHub Discussions & Releases** use the MCP Integrator with GitHub's MCP server and the same
token. The UI keeps the Integrator's rules: a preview of sample documents and coverage first,
nothing indexed until you enable it. Other Integrator providers are listed as *early access* and
are set up from the command line ([integrator.md](./integrator.md)); their status, errors and
actions appear in the UI once added.

## Asking

Questions go through `search_context`; each hit is opened with `get_document`. For every source
the UI shows the exact supporting lines (the shortest window in the hit that mentions the most
question terms, extended to finish a sentence or include an adjacent outcome line such as
"Closed: we decided to keep SQS."), the terms it mentions, its state and dates, an in-app view
with those lines highlighted, and a link to the source: commit-pinned with line anchors for files,
the page itself for issues and PRs.

- **Source-backed search** (default). Sources that mention at least half of the question's topic
  terms are evidence; those mentioning more terms come first, then search order. An identifier such
  as `billing-api` is one term and matches exactly (not `billing-api-v2`); in YAML the excerpt
  includes the key the lines belong to (`orders-service:` above its `depends_on`).
- **Insufficient evidence.** When nothing qualifies, the UI says so and does not guess. Weak
  matches are listed separately, collapsed.
- **Grounded answers** are offered only when Bedrock is configured (embedding provider `bedrock`)
  and the model answers. `AQUIFER_WEB__ANSWER_MODEL_ID` (default: the extraction model) writes at
  most four sentences from the excerpts only, citing each one; an answer with a missing or unknown
  citation is not shown. If the model call fails, the UI falls back to search and says why. Turn
  answers off with `AQUIFER_WEB__ANSWERS_ENABLED=false`.

## Agents

The Agents page is where an MCP client gets connected ([agent-integration.md](./agent-integration.md)):

1. **Endpoint**: the MCP address for this environment, its transport (Streamable HTTP) and
   authentication. On the AWS private profile it explains that `127.0.0.1:18080` is the SSM
   tunnel on this computer, and what an agent elsewhere needs (its own tunnel, or HTTPS access).
2. **Connect an agent** (admins): name the agent; Aquifer creates an **agent credential** and
   shows it once (masked, with copy); setup for the tested clients (Claude Code, the MCP Python
   SDK) reads it from the `AQUIFER_TOKEN` environment variable, never inlined; then a curl probe
   and a live status that turns **Verified** only when Aquifer receives an authenticated tool
   call from that credential.
3. **Credentials**: every credential with its role (admin: UI and MCP; agent: MCP, read-only),
   status (used in 7 days, idle, never used, revoked), last tool call and tools used. Revoking
   applies immediately.

Agent credentials call the read-only MCP tools only: they cannot sign in to the UI and never
take an admin principal's name. Tool calls are recorded per credential, tool and day in
`<data_dir>/usage.db` (UI queries are not counted); creation facts are in
`<data_dir>/agent-tokens.json` (no secrets). Aquifer identifies callers by credential only, not
by person or agent product. When tokens come from the deployment's environment
(`AQUIFER_MCP__AUTH_TOKENS`, e.g. the scale profile), the page shows them but does not manage them.

The home page's **Agent credentials** card counts active agent credentials and how many made a
tool call in the last 7 days; neither is a count of live connections.

## Home and Settings

- **Sources** card: *Up to date* only after a successful sync with no failing source, with the
  time of the last successful sync; otherwise *Syncing*, *Sync failed* (with the last success)
  or *Not synced yet*.
- **Answers** card: *With citations* when a model is available, else *Search only*. Citations
  show where each statement comes from; they do not make it correct. Model identifiers are on
  the **Settings** page (models, access mode, authentication, admin principals, sync interval).
- Suggested questions are built from indexed content without a model: the most depended-on
  Compose service, pull requests that move "from X to Y", and where decision records live.
- Times are shown relative to the server's clock (read from each API response), so a skewed
  laptop clock does not shift them.

## Branding

Logo, favicon and colors follow aquifer-ai.dev: the round strata logo (`static/logo-*.png`,
`favicon-32.png`, from the site's own assets), the wordmark "Aquifer" with a mono "AI", Inter and
JetBrains Mono when installed. The dark theme uses the site's tokens as published (cyan
`hsl(187 84% 56%)`, mineral blue, deep navy); the light theme keeps the hues and darkens cyan
text to `#0b7583` for WCAG AA contrast. Primary buttons are cyan with deep-navy text in both
themes. All colors are tokens at the top of `static/app.css`.

## The demo workspace

*Explore the demo* opens a separate, clearly labeled workspace with the synthetic `acme/*`
repositories (bundled as `src/aquifer/web/demo_corpus.json`; rebuild with
`python scripts/build_demo_corpus.py` after editing `examples/demo/repos`). It has its own
in-memory index, makes no model calls during indexing, never sees your sources, and marks its
GitHub links as fictional. Its Sources page (`#/sources?ws=demo`) lists the demo repositories and what
was indexed from each, labeled *Demo data*. Turn it off with `AQUIFER_WEB__DEMO_ENABLED=false`.
