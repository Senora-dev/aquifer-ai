"""Bundle the synthetic demo repositories (examples/demo/repos) into the package.

The UI's labeled demo workspace reads ``src/aquifer/web/demo_corpus.json``, so it works in every
install (wheel, Docker, the AWS server) without the examples directory. Re-run after editing the
demo data; ``tests/test_web.py`` fails while the bundle is out of date.

    python scripts/build_demo_corpus.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquifer.web.demo import CORPUS_PATH, build_corpus  # noqa: E402


def main() -> None:
    corpus = build_corpus(ROOT / "examples" / "demo" / "repos")
    CORPUS_PATH.write_text(json.dumps(corpus, indent=1, sort_keys=True) + "\n")
    print(f"wrote {CORPUS_PATH.relative_to(ROOT)} ({len(corpus['documents'])} documents)")


if __name__ == "__main__":
    main()
