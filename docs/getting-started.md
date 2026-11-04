# Getting Started

There are two ways to run Aquifer:

| | Local mode | AWS deployment |
| --- | --- | --- |
| Needs | Python 3.11+ | AWS account, Bedrock model access, Docker, Node.js, CDK |
| Sources | Repository directories on disk | GitHub repositories |
| Embeddings | Lexical hashing embedder (deterministic, not semantic) | Amazon Titan Text Embeddings v2 |
| Extraction | Deterministic parsers only (unless `--bedrock`) | Deterministic parsers + LLM on your Bedrock |
| Index | JSON file | OpenSearch Serverless (VPC-only) |
| MCP transport | stdio (or loopback HTTP) | Streamable HTTP + bearer token, internal NLB |
| Cost | Free | See [operations.md](./operations.md#cost) |

Local mode runs the same pipeline, extractors and MCP tool code as a deployment. Use it to
evaluate Aquifer, develop, and demo; use the AWS deployment for real repositories and semantic
search.

## Local mode

```bash
git clone https://github.com/Senora-dev/aquifer-ai.git && cd aquifer-ai
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Index every repository directory under a root (re-run any time; unchanged files are skipped)
python -m aquifer.local ingest examples/demo/repos
# → indexed=20 unchanged=0 failed=0 deleted=0 store=.aquifer-local/index.json
```

Serve it to an MCP client over stdio (the client starts the process):

```bash
python -m aquifer.local serve --repos-root examples/demo/repos
```

or over loopback HTTP at `http://127.0.0.1:8080/mcp` (no auth, loopback only):

```bash
python -m aquifer.local serve --repos-root examples/demo/repos --transport http
```

Connect a client: [agent-integration.md](./agent-integration.md). Guided demo:
[../examples/demo/README.md](../examples/demo/README.md).

### Your own repositories

Point `ingest` at a directory whose subdirectories are repository checkouts. Files matching the
default globs (Terraform, docker-compose, Backstage descriptors, ADRs) and the README are
indexed. For each repository you may add `.aquifer/source.json`:

```json
{"repo": "your-org/your-repo", "url_base": "https://github.com/your-org/your-repo"}
```

`revision` is read from `git rev-parse HEAD` for git checkouts (or from `source.json`); without
either, it is reported as unknown rather than guessed. Issues/PRs can be supplied as
`.aquifer/issues.json` / `.aquifer/pulls.json` fixtures (see the demo repositories).

`--bedrock` switches local mode to Bedrock embeddings and LLM extraction (uses your AWS
credentials and costs money).

## AWS deployment

The default is one small server (~$20/month in eu-west-1, plus model usage):

```bash
cd deploy/aws
./aquifer-aws check
AQUIFER_REPOS='["your-org/your-repo"]' ./aquifer-aws configure
./aquifer-aws plan && ./aquifer-aws up && ./aquifer-aws connect
```

See [deploy/aws/README.md](../deploy/aws/README.md). To run on infrastructure you already have
(Docker) or on the OpenSearch scale profile, see [deployment.md](./deployment.md).

---

**Support.** For deployment assistance, professional advisory, or commercial inquiries, contact
**[Senora.dev/contact](https://Senora.dev/contact)**.
