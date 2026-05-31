from aquifer.core.models import Document, DocumentKind, SourceType
from aquifer.semantic.prompts import (
    BASE_SYSTEM_PROMPT,
    PromptRegistry,
    PromptTemplate,
    default_registry,
)


def _doc(kind=DocumentKind.PR, source=SourceType.GITHUB, body="x", title="t"):
    return Document(
        id="d",
        source_type=source,
        external_id="o/r#1",
        kind=kind,
        repo="o/r",
        title=title,
        body=body,
        labels=["bug"],
    )


def test_registry_exact_match_per_kind():
    reg = default_registry()
    assert reg.select(SourceType.GITHUB, DocumentKind.PR).name == "github-code"
    assert reg.select(SourceType.GITHUB, DocumentKind.ISSUE).name == "github-issue"
    assert reg.select(SourceType.GITHUB, DocumentKind.README).name == "github-readme"


def test_registry_source_level_fallback_for_jira():
    reg = default_registry()
    # Jira is registered at the source level, so any Jira kind uses the task template.
    assert reg.select(SourceType.JIRA, DocumentKind.ISSUE).name == "jira-task"
    assert reg.select(SourceType.JIRA, DocumentKind.COMMENT).name == "jira-task"


def test_registry_github_source_default_for_unmapped_kind():
    reg = default_registry()
    # COMMENT has no exact GitHub template → falls back to the GitHub source default (code).
    assert reg.select(SourceType.GITHUB, DocumentKind.COMMENT).name == "github"


def test_registry_generic_default_for_unknown_source(monkeypatch):
    reg = PromptRegistry()  # empty registry
    assert reg.select(SourceType.GITHUB, DocumentKind.PR).name == "generic"


def test_render_user_includes_metadata_and_truncates_body():
    tmpl = default_registry().select(SourceType.GITHUB, DocumentKind.PR)
    doc = _doc(body="A" * 500, title="Add caching")
    rendered = tmpl.render_user(doc, max_chars=100)

    assert "pull request or source change" in rendered  # github-code instructions
    assert "title: Add caching" in rendered
    assert "labels: bug" in rendered
    # Body truncated to max_chars (100 A's present, 101 not).
    assert ("A" * 100) in rendered
    assert ("A" * 101) not in rendered


def test_custom_template_registration_overrides():
    reg = default_registry()
    custom = PromptTemplate(name="my-prs", instructions="custom")
    reg.register(custom, source_type=SourceType.GITHUB, kind=DocumentKind.PR)
    assert reg.select(SourceType.GITHUB, DocumentKind.PR).name == "my-prs"


def test_templates_share_base_system_prompt():
    tmpl = default_registry().select(SourceType.GITHUB, DocumentKind.PR)
    assert tmpl.system == BASE_SYSTEM_PROMPT
    assert "single valid JSON object" in tmpl.system
