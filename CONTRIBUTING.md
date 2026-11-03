# Contributing to Aquifer

Thanks for helping! You do **not** need an AWS account to develop or test Aquifer.

```bash
git clone https://github.com/Senora-dev/aquifer-ai.git && cd aquifer-ai
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"                 # add ",cdk" (and Node.js 22) to run the CDK synth tests

ruff check src tests scripts infrastructure
pytest -q                               # everything runs offline with fakes / in-memory stores
python scripts/benchmark_retrieval.py   # retrieval benchmark, deterministic and offline

# Try the whole product locally (no AWS, no network):
python -m aquifer.local ingest examples/demo/repos
python -m aquifer.local serve --repos-root examples/demo/repos   # MCP over stdio
```

Tests that need the CDK libraries skip automatically when `aws-cdk-lib` is not installed. Live
AWS checks are opt-in only (`scripts/preflight.py`, `--bedrock` benchmark mode).

## Ground rules

1. **Neutrality.** Aquifer returns facts and where they came from. No risk scores,
   recommendations or decisions — those belong in the agent.
2. **Provenance is never invented.** Unknown line numbers, revisions or timestamps are `null`.
3. **Lean core.** The core depends on interfaces; adapters are wired in
   `aquifer/ingestion/factory.py`.
4. **No secrets or private data** in code, fixtures, logs or examples. Use fictional
   organizations (e.g. `acme/...`) and placeholder account ids.

## Where to start

The extension guide — adding a connector, an interceptor, a structured extractor, or a vector
store — is in [docs/contributing.md](docs/contributing.md). Architecture: [docs/architecture.md](
docs/architecture.md).

## Pull requests

Keep PRs focused, include tests that run offline, and update `CHANGELOG.md` for user-visible
changes. CI runs lint, tests, the offline benchmark, a dependency audit and an image build.

## License

Aquifer is source-available under the Business Source License 1.1 (see `LICENSE` and
`LICENSE_TERMS.md`). By contributing you agree that your contributions are licensed under the
same terms.
