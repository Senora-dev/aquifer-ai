"""CDK stack: synthesizes, validates its config, and keeps the security properties we rely on.

Skipped when aws-cdk-lib (and Node.js, which jsii needs) are not installed. No AWS calls are made
and Docker bundling is disabled for the synth.
"""

import json
import sys
from pathlib import Path

import pytest

cdk = pytest.importorskip("aws_cdk")
from aws_cdk.assertions import Match, Template  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infrastructure"))
from aquifer_stack import AquiferStack  # noqa: E402
from stack_config import ConfigError, bedrock_model_arns, load_config  # noqa: E402

ACCOUNT, REGION = "111111111111", "eu-west-1"  # placeholder account id, not a real one


def _synth(**context):
    ctx = {"repos": '["acme/shop"]', "aws:cdk:bundling-stacks": [], **context}
    app = cdk.App(context=ctx)
    stack = AquiferStack(app, "Test", env=cdk.Environment(account=ACCOUNT, region=REGION))
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def template():
    return _synth()


@pytest.fixture(scope="module")
def resources(template):
    return template.to_json()["Resources"]


def _of_type(resources, type_):
    return [r for r in resources.values() if r["Type"] == type_]


def test_mcp_load_balancer_is_internal(template):
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::LoadBalancer", {"Scheme": "internal", "Type": "network"}
    )


def test_no_security_group_is_open_to_the_internet(resources):
    for sg in _of_type(resources, "AWS::EC2::SecurityGroup"):
        for rule in sg["Properties"].get("SecurityGroupIngress", []):
            assert rule.get("CidrIp") != "0.0.0.0/0"
    for rule in _of_type(resources, "AWS::EC2::SecurityGroupIngress"):
        assert rule["Properties"].get("CidrIp") != "0.0.0.0/0"


def test_application_egress_is_https_only(resources):
    for sg in _of_type(resources, "AWS::EC2::SecurityGroup"):
        for rule in sg["Properties"].get("SecurityGroupEgress", []):
            if rule.get("CidrIp") == "0.0.0.0/0":
                assert (rule["IpProtocol"], rule["FromPort"], rule["ToPort"]) == ("tcp", 443, 443)


def test_opensearch_collection_is_vpc_only(resources):
    policies = [r for r in _of_type(resources, "AWS::OpenSearchServerless::SecurityPolicy")
                if r["Properties"]["Type"] == "network"]
    assert '"AllowFromPublic":false' in json.dumps(policies[0]["Properties"]["Policy"]).replace(
        " ", "").replace("\\", "")


def test_mcp_reader_has_no_write_permission(resources):
    access = _of_type(resources, "AWS::OpenSearchServerless::AccessPolicy")[0]
    policy = json.dumps(access["Properties"]["Policy"])
    assert "MCP server: read-only" in policy
    assert "aoss:*" not in policy


def test_bedrock_is_scoped_to_configured_models(resources):
    for pol in _of_type(resources, "AWS::IAM::Policy"):
        for stmt in pol["Properties"]["PolicyDocument"]["Statement"]:
            actions = stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]]
            if "bedrock:InvokeModel" in actions:
                assert stmt["Resource"] != "*"
                assert "foundation-model" in json.dumps(stmt["Resource"])


def test_mcp_tokens_are_injected_as_a_secret_not_plaintext(resources):
    task = _of_type(resources, "AWS::ECS::TaskDefinition")[0]
    container = task["Properties"]["ContainerDefinitions"][0]
    assert any(s["Name"] == "AQUIFER_MCP__AUTH_TOKENS" for s in container.get("Secrets", []))
    assert all(e["Name"] != "AQUIFER_MCP__AUTH_TOKENS" for e in container["Environment"])
    assert container["ReadonlyRootFilesystem"] is True


def test_outputs_never_expose_secret_values(template):
    outputs = json.dumps(template.to_json()["Outputs"])
    assert "SecretString" not in outputs
    assert "resolve:secretsmanager" not in outputs


def test_extraction_runs_in_the_stack_region(resources):
    fns = _of_type(resources, "AWS::Lambda::Function")
    env = fns[0]["Properties"]["Environment"]["Variables"]
    assert env["AQUIFER_SEMANTIC_INDEX__REGION"] == REGION
    assert env["AQUIFER_EMBEDDING__REGION"] == REGION


def test_queue_timeouts_and_dlq_alarm(template):
    template.has_resource_properties(
        "AWS::SQS::Queue",
        {"VisibilityTimeout": 1800, "RedrivePolicy": Match.object_like({"maxReceiveCount": 5})},
    )
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm", {"MetricName": "ApproximateNumberOfMessagesVisible"}
    )


def test_standby_replicas_can_be_disabled_for_dev():
    t = _synth(aoss_standby_replicas="DISABLED")
    t.has_resource_properties("AWS::OpenSearchServerless::Collection",
                              {"StandbyReplicas": "DISABLED"})


def test_tls_listener_with_certificate():
    t = _synth(mcp_certificate_arn="arn:aws:acm:eu-west-1:111111111111:certificate/abc")
    t.has_resource_properties("AWS::ElasticLoadBalancingV2::Listener",
                              {"Port": 443, "Protocol": "TLS"})


def _get(**ctx):
    return lambda key: ctx.get(key)


def test_config_requires_repositories():
    with pytest.raises(ConfigError, match="No repositories"):
        load_config(_get())


def test_config_rejects_wildcards_and_public_cidrs():
    with pytest.raises(ConfigError, match="wildcards"):
        load_config(_get(repos='["acme/*"]'))
    with pytest.raises(ConfigError, match="private by design"):
        load_config(_get(repos="acme/shop", mcp_allowed_cidrs="0.0.0.0/0"))


def test_config_accepts_comma_lists_and_json():
    assert load_config(_get(repos="a/b, c/d")).repos == ["a/b", "c/d"]
    assert load_config(_get(repos='["a/b"]', mcp_allowed_cidrs='["10.1.0.0/16"]'))\
        .mcp_allowed_cidrs == ["10.1.0.0/16"]


def test_inference_profile_arns():
    arns = bedrock_model_arns("us.anthropic.claude-x-v1:0", "us-east-1", "123")
    assert arns[0] == "arn:aws:bedrock:us-east-1:123:inference-profile/us.anthropic.claude-x-v1:0"
    assert arns[1] == "arn:aws:bedrock:*::foundation-model/anthropic.claude-x-v1:0"
