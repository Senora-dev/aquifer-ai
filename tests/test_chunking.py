from aquifer.core.chunking import (
    chunk_document,
    chunk_text,
    chunk_text_with_overlap,
    estimate_tokens,
)
from aquifer.core.models import (
    Document,
    DocumentKind,
    Entity,
    Relationship,
    SemanticMetadata,
    SourceType,
)


def test_empty_text_yields_no_chunks():
    assert chunk_text("", chunk_size=100, overlap=10) == []
    assert chunk_text("   \n  ", chunk_size=100, overlap=10) == []


def test_short_text_is_a_single_chunk():
    chunks = chunk_text("hello world", chunk_size=100, overlap=10)
    assert chunks == ["hello world"]


def test_long_text_splits_into_multiple_chunks():
    words = " ".join(f"word{i}" for i in range(500))
    chunks = chunk_text(words, chunk_size=50, overlap=10)
    assert len(chunks) > 1
    # No chunk should greatly exceed the target size.
    for c in chunks:
        assert estimate_tokens(c) <= 50 + 20  # small slack for word boundaries


def test_chunks_overlap():
    words = " ".join(f"w{i}" for i in range(200))
    chunks = chunk_text(words, chunk_size=40, overlap=10)
    # The end of one chunk should reappear at the start of the next.
    first_tail = chunks[0].split()[-1]
    assert first_tail in chunks[1].split()


def test_overlap_must_be_smaller_than_chunk_size():
    try:
        chunk_text("a b c", chunk_size=10, overlap=10)
    except ValueError:
        return
    raise AssertionError("expected ValueError when overlap >= chunk_size")


def test_chunk_document_prepends_title_and_denormalizes_fields():
    doc = Document(
        id="github:org/repo:issue:1",
        source_type=SourceType.GITHUB,
        external_id="org/repo#1",
        kind=DocumentKind.ISSUE,
        repo="org/repo",
        title="Login is broken",
        body="Users cannot authenticate after the deploy.",
        url="https://github.com/org/repo/issues/1",
        labels=["bug"],
    )
    chunks = chunk_document(doc, chunk_size=100, overlap=10)
    assert len(chunks) == 1
    chunk = chunks[0]
    assert "Login is broken" in chunk.text
    assert chunk.chunk_id == "github:org/repo:issue:1#0"
    assert chunk.document_id == doc.id
    assert chunk.repo == "org/repo"
    assert chunk.labels == ["bug"]
    assert chunk.url == doc.url


def test_chunk_document_denormalizes_semantic_metadata_onto_every_chunk():
    words = " ".join(f"w{i}" for i in range(300))
    doc = Document(
        id="github:org/repo:pr:1",
        source_type=SourceType.GITHUB,
        external_id="org/repo#1",
        kind=DocumentKind.PR,
        repo="org/repo",
        title="Add cache",
        body=words,
        semantic=SemanticMetadata(
            summary="adds caching",
            entities=[
                Entity(type="service", name="billing-api"),
                Entity(type="datastore", name="redis"),
            ],
            topics=["caching"],
            relationships=[Relationship(type="depends_on", target="redis")],
        ),
    )
    chunks = chunk_document(doc, chunk_size=40, overlap=10)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.summary == "adds caching"
        assert [e.name for e in chunk.entities] == ["billing-api", "redis"]
        assert chunk.relationships[0].target == "redis"


def test_chunk_document_without_semantic_metadata_has_empty_fields():
    doc = Document(
        id="d",
        source_type=SourceType.GITHUB,
        external_id="org/repo#1",
        kind=DocumentKind.ISSUE,
        title="t",
        body="hello world",
    )
    chunk = chunk_document(doc, chunk_size=100, overlap=10)[0]
    assert chunk.summary == ""
    assert chunk.entities == []
    assert chunk.relationships == []


def test_chunks_record_their_overlap_with_the_previous_chunk():
    words = " ".join(f"w{i}" for i in range(200))
    chunks = chunk_text_with_overlap(words, chunk_size=40, overlap=10)
    assert chunks[0][1] == 0  # nothing precedes the first chunk
    for text, overlap_words in chunks[1:]:
        # Every later chunk is seeded with context, and never with more than it holds.
        assert 0 < overlap_words < len(text.split())


def test_recorded_overlap_matches_the_previous_chunk_tail():
    words = " ".join(f"word{i:03d}" for i in range(400))
    chunks = chunk_text_with_overlap(words, chunk_size=60, overlap=15)
    for (prev_text, _), (text, overlap_words) in zip(chunks, chunks[1:], strict=False):
        prev_words = prev_text.split()
        assert text.split()[:overlap_words] == prev_words[len(prev_words) - overlap_words :]


def test_dropping_the_recorded_overlap_restores_the_original_words():
    words = [f"w{i}" for i in range(500)]
    chunks = chunk_text_with_overlap(" ".join(words), chunk_size=50, overlap=12)
    rebuilt: list[str] = []
    for position, (text, overlap_words) in enumerate(chunks):
        parts = text.split()
        rebuilt.extend(parts[overlap_words:] if position else parts)
    assert rebuilt == words


def test_chunk_text_still_returns_plain_strings():
    words = " ".join(f"w{i}" for i in range(200))
    assert chunk_text(words, chunk_size=40, overlap=10) == [
        text for text, _ in chunk_text_with_overlap(words, chunk_size=40, overlap=10)
    ]


def test_chunk_document_records_overlap_on_each_chunk():
    doc = Document(
        id="d",
        source_type=SourceType.GITHUB,
        external_id="org/repo#1",
        kind=DocumentKind.ISSUE,
        title="t",
        body=" ".join(f"w{i}" for i in range(300)),
    )
    chunks = chunk_document(doc, chunk_size=40, overlap=10)
    assert len(chunks) > 1
    assert chunks[0].overlap_words == 0
    assert all(c.overlap_words > 0 for c in chunks[1:])


def _file_doc(body, title="cfg.yaml"):
    return Document(id="d", source_type=SourceType.GITHUB, external_id="e",
                    kind=DocumentKind.FILE, title=title, body=body)


def test_chunks_preserve_formatting_and_reassemble_exactly():
    from aquifer.core.chunking import combined_text, reassemble

    body = "\n".join(f"key{i}:\n  - value {i}   # comment" for i in range(200))
    doc = _file_doc(body)
    chunks = chunk_document(doc, chunk_size=60, overlap=15)
    assert len(chunks) > 3
    assert "\n  - value" in chunks[0].text  # line structure kept
    text, _ = combined_text(doc)
    assert reassemble([c.model_dump() for c in chunks]) == text


def test_reassembly_is_exact_without_overlap():
    from aquifer.core.chunking import combined_text, reassemble

    doc = _file_doc("a\n\n  b c\td\n" * 50)
    chunks = chunk_document(doc, chunk_size=10, overlap=0)
    assert reassemble([c.model_dump() for c in chunks]) == combined_text(doc)[0].strip()


def test_chunk_line_ranges_point_into_the_body():
    body = "\n".join(f"line{i} " + "word " * 20 for i in range(1, 41))
    chunks = chunk_document(_file_doc(body), chunk_size=80, overlap=10)
    lines = body.split("\n")
    for c in chunks:
        assert c.line_start <= c.line_end
        # Every word of the chunk lies within the reported body lines.
        span = "\n".join(lines[c.line_start - 1 : c.line_end])
        body_part = c.text.split("\n\n", 1)[-1] if c.index == 0 else c.text
        assert body_part.strip().split()[0] in span
    assert chunks[0].line_start == 1 and chunks[-1].line_end == 40


def test_oversized_token_is_split_below_the_embedding_limit():
    blob = "A" * 200_000  # e.g. a base64 blob with no whitespace
    chunks = chunk_document(_file_doc(blob, title=""), chunk_size=800, overlap=100)
    assert len(chunks) > 1
    assert max(len(c.text) for c in chunks) <= 800 * 4


def test_title_only_chunk_has_no_line_range():
    chunks = chunk_document(_file_doc("", title="Only a title"), chunk_size=100, overlap=10)
    assert chunks[0].line_start is None
