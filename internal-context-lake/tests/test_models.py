from aquifer.core.models import Chunk, Document, DocumentKind, SourceType


def test_document_make_id_is_deterministic():
    a = Document.make_id(SourceType.GITHUB, "org/repo", DocumentKind.ISSUE, "123")
    b = Document.make_id(SourceType.GITHUB, "org/repo", DocumentKind.ISSUE, "123")
    assert a == b == "github:org/repo:issue:123"


def test_chunk_make_id():
    assert Chunk.make_id("github:org/repo:issue:123", 2) == "github:org/repo:issue:123#2"


def test_document_uses_enum_values():
    doc = Document(
        id="x",
        source_type=SourceType.GITHUB,
        external_id="org/repo#1",
        kind=DocumentKind.PR,
    )
    # use_enum_values means the stored value is the raw string, ready for JSON/index.
    dumped = doc.model_dump()
    assert dumped["source_type"] == "github"
    assert dumped["kind"] == "pr"
