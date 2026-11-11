# Instructions for AI coding agents

This file tells an AI agent how to work on, test and evaluate Aquifer. Humans: see README.md.

## What this repository is

Aquifer is a self-hosted engineering-context layer served over MCP. Python package in
`src/aquifer/`. Default AWS deployment (one server, Terraform) in `deploy/aws/`, container for
existing infrastructure in `Dockerfile.server` + `deploy/docker/`, scale profile (CDK,
OpenSearch Serverless) in `infrastructure/`. It returns neutral facts with provenance — never add scores, risk
assessments or recommendations to responses.

## Setup (no AWS needed)

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"      # add ",cdk" for CDK synth tests
```

## Verify a change

Run these before declaring work done; all run offline without credentials:

```bash
.venv/bin/ruff check src tests scripts infrastructure deploy/aws/tests
.venv/bin/pytest -q                                   # ~310 tests, ~60 s with CDK installed
.venv/bin/python scripts/benchmark_retrieval.py       # must report "failed": 0
```

Try the product locally: `python -m aquifer.local ingest examples/demo/repos`, then
`python -m aquifer.local serve --repos-root examples/demo/repos` (MCP over stdio). The browser UI:
`python -m aquifer.local ui` (http://127.0.0.1:8080/; docs/ui.md). After editing
`examples/demo/repos`, run `python scripts/build_demo_corpus.py` (the UI's bundled demo).

## Setting Aquifer up for a user (agent runbook)

Use this when a user asks you to deploy Aquifer and connect their tools. You run every command;
the user only logs in, approves, decides and clicks. Every `aquifer-aws` step prints `==>`
headings, `✓`/`✗` results and machine-readable `KEY=value` lines (`MCP_URL=`, `AUTH_URL=`,
`TOKEN_REF=`, `EXTRACTION_MODEL=`). Full guides: deploy/aws/README.md, docs/integrator.md.

### Ask the user first (product decisions: never assume)

1. **Where to run it.** The default is AWS (deploy/aws, about $20/month). If they want to use
   existing infrastructure, use Docker instead (docs/deployment.md#existing-infrastructure-docker).
   Region: default eu-west-1.
2. **Which GitHub repositories** to ingest (exact `owner/repo`). A fresh installation has none;
   the user connects them in the UI (Sources → Add a source: token, repositories, preview, enable),
   or you list them in `deploy/aws/terraform.tfvars` (`repos = [...]`) and store the token with
   `./aquifer-aws github-token`.
3. **Server size.** Ask roughly how many repositories and documents (issues, PRs, pages,
   tickets) they will index, then recommend a size and let them choose:

   | Size | Server | Baseline / month | Fits |
   | --- | --- | --- | --- |
   | `small` | t4g.small, 2 GiB | ~$19 | up to ~50 repositories / ~5,000 documents (measured) |
   | `medium` | t4g.medium, 4 GiB | ~$33 | up to ~200 repositories / ~20,000 documents (estimated) |
   | `large` | t4g.large, 8 GiB | ~$62, **above the $50 budget** | more (estimated); needs their explicit OK |

   Beyond `large`, the scale profile (docs/deployment.md) fits better. It does not run the MCP
   Integrator. The size can be changed later: `./aquifer-aws resize SIZE`.
4. **Which other tools** to connect, and what content from each.
5. **Access:** everything ingested, including content imported from other tools, is readable by
   **every** Aquifer MCP token of this deployment. It does not follow per-user permissions in
   GitHub or the tool. Get explicit agreement, or a narrower selection.

### Steps, with the points where you must stop

| # | You run | Stop and hand over to the user when |
| --- | --- | --- |
| 1 | `.venv/bin/aquifer install aws --check-only --region <region>` (from the repository root, after `pip install -e ".[server]"`) | Any `✗` blocker: AWS login → they run `aws sso login`; Bedrock access, quotas → they fix it in the AWS console (the message says where); you cannot. Missing tools → the printed install line |
| 2 | `.venv/bin/aquifer install aws --size <size> --plan-only --yes --no-browser` | **Always.** Show the plan summary, the settings (account, region, access, state location) and the cost lines, and get explicit approval. Never pick `large` without their OK |
| 3 | `.venv/bin/aquifer install aws --size <size> --yes --no-browser` (~5 min; makes a fresh plan, applies it, waits for health, opens the SSM tunnel) | If it stops, read the `✗` line and re-run the same command (it resumes) |
| 4 | Give the user `UI_URL=` and how to sign in (`deploy/aws/aquifer-aws token \| pbcopy`). Agents: each gets its own read-only credential (UI → Agents → Connect an agent; docs/agent-integration.md). Never put a token on a command line, in a URL or in a committed file | — |
| 5 | Per tool: `./aquifer-aws sources providers`; then `sources add …` | Token tools: `./aquifer-aws source-token NAME --from-gh` for GitHub; for others the user pipes the token (`./aquifer-aws source-token NAME < file`). Never ask them to paste a secret into chat, and never put one on a command line |
| 6 | OAuth tools: `./aquifer-aws authorize NAME` | **Always.** Give the user the `AUTH_URL=` link immediately: they open it in a browser **on the same computer** (the tunnel carries the redirect) and approve. The command waits up to ~15 minutes; if it times out, run it again for a new link |
| 7 | `sources template …` (preconfigured streams) or `sources propose …` (any server), then `sources preview NAME` | **Always.** Show the samples, the coverage lines (what changes and deletions it can detect) and the access statement; get approval before enabling |
| 8 | `sources enable NAME`, then `sources status` until `last run succeeded` | — |

**Done** means:
- `./aquifer-aws status` shows no dead letters and a last sync without error
- every source in `sources status` is `active` with a successful last run
- one question in the user's MCP client returns cited results; check with `connect` +
  `search_context`

Then tell the user the monthly cost, how to remove everything (`./aquifer-aws down`, which only
they ask for), and that sources re-sync daily.

### Typical problems

| Symptom | Do |
| --- | --- |
| `sources status`: `needs_authorization` / `needs_reauth` | OAuth: rerun step 6. Token: store a new token (step 5), then `sources add NAME … --replace` |
| `plan_invalid` | The server's tools changed: `sources tools NAME`, then fix the stream (`template`/`propose`) |
| `unsafe_destination` | The URL is not public HTTPS. Ask the user before setting `AQUIFER_INTEGRATOR__ALLOW_PRIVATE_NETWORKS` |
| A tool shows as `write` or `unknown` | It cannot be used. Do not try to work around the read-only gate |
| Dead letters | Read the error in `status`; after fixing the cause: `./aquifer-aws run dead-letters --requeue all` |
| Extraction `failed` after fixing Bedrock access | `./aquifer-aws retry-failed` |

### Never

- Run `up` or `aquifer install aws` without `--plan-only` before approval, or `down` / `terraform destroy` unless the user asks.
- Enable a source without showing its preview, or connect content the user did not choose.
- Print, log or commit tokens, or pass them as arguments.
- Edit the plan to call a tool the gate refuses.

## Evaluating Aquifer on AWS (developers)

`./aquifer-aws seed` creates synthetic private test repositories. Then:
- `test`: 25 checks
- `test-updates`: commits a change and a deletion, then reverts them
- `test-integrator`: GitHub MCP end to end, including fault and crash recovery; its mutations go
  only to the fixture repository

The same approval rule applies before `up`. Failure triage: `./aquifer-aws status`,
`./aquifer-aws logs 30`.

## Rules

- Never invent provenance (line numbers, revisions, timestamps): unknown values are `null`.
- No secrets, AWS account ids, customer data or private repository content in code, fixtures,
  logs or docs. Use fictional orgs (`acme/...`) and placeholder ids (`111111111111`).
- Keep tool names and required parameters stable; add only optional fields.
- New behavior needs offline tests (fakes / `InMemoryVectorStore`), and benchmark cases if it
  affects retrieval.
- License is BSL 1.1 (source-available) — do not describe it as open source or change it.
