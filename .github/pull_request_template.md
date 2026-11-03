## What and why

<!-- One or two sentences. Link the issue if there is one. -->

## How it was tested

- [ ] `ruff check src tests scripts infrastructure`
- [ ] `pytest -q` (no AWS credentials needed)
- [ ] `python scripts/benchmark_retrieval.py` if retrieval, extraction or chunking changed

## Checklist

- [ ] Responses stay neutral (facts and provenance, no scores or recommendations)
- [ ] No secrets, account ids, customer data or private repository content in code, fixtures or logs
- [ ] Docs / CHANGELOG updated if behavior changed
