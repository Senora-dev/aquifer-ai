"""Deterministic extraction: explicit references with exact line evidence; no co-occurrence."""

from aquifer.core.models import Document, DocumentKind, Relationship, SemanticMetadata, SourceType
from aquifer.semantic.structured import extract_structured, ground_llm_metadata, merge_metadata


def _file(path, body, repo="acme/infra"):
    return Document(
        id=f"github:{repo}:file:{path}", source_type=SourceType.GITHUB, external_id=path,
        kind=DocumentKind.FILE, repo=repo, path=path, revision="abc123", title=path, body=body,
        url=f"https://github.com/{repo}/blob/abc123/{path}",
    )


TF = '''# aws_db_instance.legacy is only mentioned in this comment
module "billing" {
  source  = "../modules/billing-service"
  db_host = aws_db_instance.payments.address
  note    = "see aws_db_instance.not_a_ref"
  queue   = "${module.queues.billing_url}"
  depends_on = [module.network]
}

resource "aws_db_instance" "payments" {
  identifier = "payments-db"
}
'''


def _edges(meta):
    return {(r.source, r.type, r.target): r for r in meta.relationships}


def test_terraform_references_have_exact_lines_and_explicit_origin():
    meta = extract_structured(_file("envs/prod/main.tf", TF))
    edges = _edges(meta)
    ref = edges[("module.billing", "references", "aws_db_instance.payments")]
    assert ref.origin == "explicit_reference" and ref.status == "current"
    ev = ref.evidence[0]
    assert (ev.line_start, ev.line_end, ev.revision, ev.path) == (4, 4, "abc123",
                                                                   "envs/prod/main.tf")
    assert ev.url.endswith("main.tf#L4")
    assert ("module.billing", "depends_on", "module.network") in edges
    assert ("module.billing", "references", "module.queues") in edges  # inside ${}
    src = edges[("module.billing", "references", "envs/modules/billing-service")]
    assert src.description == "module source" and src.evidence[0].line_start == 3


def test_terraform_ignores_names_in_comments_and_plain_strings():
    targets = {r.target for r in extract_structured(_file("main.tf", TF)).relationships}
    assert "aws_db_instance.legacy" not in targets
    assert "aws_db_instance.not_a_ref" not in targets


def test_terraform_entities_are_namespaced_by_module_directory():
    meta = extract_structured(_file("envs/prod/main.tf", TF))
    res = next(e for e in meta.entities if e.name == "aws_db_instance.payments")
    assert res.namespace == "acme/infra:envs/prod"
    assert (res.evidence[0].line_start, res.evidence[0].line_end) == (10, 12)


COMPOSE = """services:
  billing-api:
    image: acme/billing
    depends_on:
      - payments-db
  payments-db:
    image: postgres
  worker:
    depends_on:
      billing-api:
        condition: service_healthy
"""


def test_compose_depends_on_list_and_mapping_forms():
    edges = _edges(extract_structured(_file("docker-compose.yml", COMPOSE)))
    assert edges[("billing-api", "depends_on", "payments-db")].evidence[0].line_start == 5
    assert edges[("worker", "depends_on", "billing-api")].evidence[0].line_start == 10


def test_compose_services_without_depends_on_create_no_edges():
    # payments-db and billing-api appear in the same file; that alone is not a dependency.
    edges = _edges(extract_structured(_file("docker-compose.yml", COMPOSE)))
    assert ("payments-db", "depends_on", "billing-api") not in edges
    assert ("billing-api", "depends_on", "worker") not in edges


BACKSTAGE = """apiVersion: backstage.io/v1alpha1
kind: Component
metadata:
  name: billing-api
spec:
  owner: team-payments
  system: payments
  dependsOn:
    - resource:default/payments-db
  consumesApis:
    - ledger-api
"""


def test_backstage_relations():
    meta = extract_structured(_file("catalog-info.yaml", BACKSTAGE, repo="acme/billing"))
    edges = _edges(meta)
    dep = edges[("billing-api", "depends_on", "payments-db")]
    assert (dep.source_type, dep.target_type, dep.evidence[0].line_start) == (
        "component", "resource", 9)
    assert ("billing-api", "consumes_api", "ledger-api") in edges
    assert ("billing-api", "part_of", "payments") in edges
    assert ("billing-api", "owned_by", "team-payments") in edges
    target = next(e for e in meta.entities if e.name == "payments-db")
    assert target.namespace is None  # Backstage "default" namespace is implicit


def test_unrecognized_or_malformed_yaml_is_ignored():
    assert extract_structured(_file("config.yaml", "services:\n  a:\n    depends_on: [b]\n"))\
        is None  # not a compose file name → not guessed
    assert extract_structured(_file("docker-compose.yml", "services: [unclosed")) is None
    assert extract_structured(_file("README.md", "billing-api depends on x")) is None


def test_llm_relationships_are_checked_against_text_without_claiming_truth():
    doc = Document(id="d", source_type=SourceType.GITHUB, external_id="o/r#1",
                   kind=DocumentKind.ISSUE, repo="o/r", title="Timeouts",
                   body="Intro\nbilling-api now calls payments-db for refunds\nend")
    meta = SemanticMetadata(relationships=[
        Relationship(type="depends_on", source="billing-api", target="payments-db",
                     origin="explicit_reference",  # the model can't claim explicit origin
                     quote="billing-api now calls payments-db for refunds"),
        Relationship(type="depends_on", target="kafka", quote="uses kafka"),  # not in text
    ])
    ground_llm_metadata(meta, doc)
    quoted, unsupported = meta.relationships
    assert quoted.origin == "llm_extracted" and quoted.status == "claimed"
    assert quoted.quote_verified is True and quoted.mention_found is True
    assert (quoted.evidence[0].line_start, quoted.evidence[0].locator) == (2, "quote")
    assert unsupported.quote_verified is False and unsupported.mention_found is False
    assert unsupported.evidence == []


def test_mention_without_quote_falls_back_to_text_match_line():
    doc = Document(id="d", source_type=SourceType.GITHUB, external_id="o/r#1",
                   kind=DocumentKind.ISSUE, repo="o/r", title="t",
                   body="line one\nwe looked at kafka last year")
    meta = SemanticMetadata(relationships=[Relationship(type="mentions", target="kafka")])
    rel = ground_llm_metadata(meta, doc).relationships[0]
    assert rel.mention_found is True and rel.quote_verified is False
    assert (rel.evidence[0].line_start, rel.evidence[0].locator) == (2, "text_match")


def test_merge_prefers_explicit_reference_on_overlap():
    explicit = extract_structured(_file("docker-compose.yml", COMPOSE))
    llm = SemanticMetadata(summary="s", relationships=[
        Relationship(type="depends_on", source="Billing-API", target="payments-db"),
        Relationship(type="mentions", target="grafana"),
    ])
    merged = merge_metadata(explicit, llm)
    by_key = {(r.source, r.type, r.target): r for r in merged.relationships}
    assert by_key[("billing-api", "depends_on", "payments-db")].origin == "explicit_reference"
    assert (None, "mentions", "grafana") in by_key
    assert merged.summary == "s"
