"""``aquifer install aws`` and the Terraform module, offline.

Terraform, AWS and the local system are fakes: these tests check what the installer decides
(blockers, settings, state handling, plan review, recovery, what it prints) without creating
anything. Live checks against AWS are described in docs/install-aws.md#verification.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from aquifer.install import terraform as tf
from aquifer.install.aws import Console, Installer, Options, System

ROOT = Path(__file__).resolve().parents[1]
SECRET = "aqf_" + "s" * 40  # an access token value: must never be printed


# --- fakes -------------------------------------------------------------------------------------

class FakeTerraform:
    """Answers the terraform/pip commands the installer runs, and records them."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.calls: list[list[str]] = []
        self.state: list[str] = []
        self.plan = {"resource_changes": [
            {"address": f"module.aquifer.{a}", "change": {"actions": ["create"]}}
            for a in ("aws_vpc.this", "aws_instance.server", "aws_ebs_volume.data",
                      "aws_s3_object.wheel", "aws_ssm_parameter.config")]}
        self.apply_rc = 0
        self.apply_text = "module.aquifer.aws_vpc.this: Creation complete after 2s [id=vpc-1]\n"
        self.name = "aquifer"
        self.repos: list[str] = []

    def outputs(self) -> dict:
        return {k: {"value": v} for k, v in {
            "instance_id": "i-0123456789abcdef0", "public_ip": "203.0.113.10",
            "ssm_prefix": f"/aquifer/{self.name}", "repos": self.repos,
            "ui_url": "http://127.0.0.1:18080/", "access_mode": "private"}.items()}

    def run(self, cmd, cwd=None, env=None):  # noqa: ANN001, ANN201
        self.calls.append(list(cmd))
        out, rc = "", 0
        if cmd[:3] == ["terraform", "version", "-json"]:
            out = json.dumps({"terraform_version": "1.17.0"})
        elif "pip" in cmd and "wheel" in cmd:
            dist = Path(cmd[cmd.index("-w") + 1])
            dist.mkdir(parents=True, exist_ok=True)
            (dist / "aquifer-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
        elif cmd[0] == "terraform" and cmd[2:4] == ["state", "list"]:
            out = "\n".join(self.state)
        elif cmd[0] == "terraform" and cmd[2:4] == ["show", "-json"]:
            out = json.dumps(self.plan)
        elif cmd[0] == "terraform" and cmd[2:4] == ["output", "-json"]:
            out = json.dumps(self.outputs())
        elif cmd[0] == "terraform" and cmd[2] == "import":
            self.state.append(cmd[-2])
        elif cmd[0] == "terraform" and cmd[2:4] == ["output", "-raw"]:
            if self.state:
                out = f"/aquifer/{self.name}"
            else:
                rc = 1
        return subprocess.CompletedProcess(cmd, rc, out, "")

    def stream(self, cmd, cwd=None, env=None, on_line=None):  # noqa: ANN001, ANN201
        self.calls.append(list(cmd))
        if cmd[2] == "plan":
            out = next(a for a in cmd if a.startswith("-out=")).split("=", 1)[1]
            Path(out).write_text("plan")
            return 0, "Plan: 5 to add"
        if cmd[2] == "apply":
            for line in self.apply_text.splitlines():
                if on_line:
                    on_line(line)
            if self.apply_rc == 0:
                self.state = [c["address"] for c in self.plan["resource_changes"]]
            return self.apply_rc, self.apply_text
        raise AssertionError(cmd)

    def commands(self, verb: str) -> list[list[str]]:
        return [c for c in self.calls if c[0] == "terraform" and len(c) > 2 and c[2] == verb]


class FakeRuntime:
    def __init__(self, deny_embedding: bool = False) -> None:
        self.deny = deny_embedding

    def invoke_model(self, **kw):  # noqa: ANN003, ANN201
        if self.deny:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "AccessDeniedException",
                                         "Message": "no access to model"}}, "InvokeModel")
        return {}

    def converse(self, **kw):  # noqa: ANN003, ANN201
        return {}


class FakeControl:
    def list_inference_profiles(self, **kw):  # noqa: ANN003, ANN201
        return {"inferenceProfileSummaries": [
            {"inferenceProfileId": "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
             "status": "ACTIVE"}]}


class FakeAws:
    def __init__(self, region: str) -> None:
        self.region = region
        self.fail_identity = False
        self.roles: set[str] = set()
        self.params: set[str] = {"/aquifer/aquifer/mcp-tokens"}
        self.remote: list[str] = []
        self.runtime = FakeRuntime()
        self.denied: list[str] = []
        self.vpcs = (2, 5)
        self.owned: dict[tuple[str, str], dict] = {}  # (kind, name) -> tags
        self.tagged: list[str] = []

    def identity(self) -> dict:
        if self.fail_identity:
            raise RuntimeError("expired")
        return {"Account": "111111111111",
                "Arn": "arn:aws:sts::111111111111:assumed-role/Admin/me"}

    def region_status(self) -> str:
        return "opt-in-not-required"

    def instance_type_offered(self, t: str) -> bool:
        return True

    def denied_actions(self, arn, actions):  # noqa: ANN001, ANN201
        return self.denied

    def role_exists(self, name: str) -> bool:
        return name in self.roles

    def parameter_exists(self, name: str) -> bool:
        return name in self.params

    def bedrock(self):  # noqa: ANN201
        return FakeControl(), self.runtime

    def vpc_headroom(self):  # noqa: ANN201
        return self.vpcs

    def eip_headroom(self):  # noqa: ANN201
        return (1, 5)

    def tags_of(self, kind: str, name: str):  # noqa: ANN201
        return self.owned.get((kind, name))

    def role_policy_exists(self, role: str, policy: str) -> bool:
        return False

    def tagged_resources(self, name: str) -> list[str]:
        return self.tagged

    def bucket_status(self, bucket: str, region: str):  # noqa: ANN201
        return (bucket == "team-tf-state", True)

    def lock_table_exists(self, table: str, region: str) -> bool:
        return table == "tf-locks"

    def instance_online(self, instance_id: str) -> bool:
        return True

    def run_remote(self, instance_id: str, command: str, timeout_s: int = 900):  # noqa: ANN201
        self.remote.append(command)
        return "Success", "aquifer healthy"


class FakePopen:
    pid = 4242

    def __init__(self, cmd, **kw) -> None:  # noqa: ANN001, ANN003
        self.cmd = cmd
        self.kw = kw


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "aquifer-ai"
    (repo / "deploy" / "aws" / "modules" / "aquifer").mkdir(parents=True)
    (repo / "deploy" / "aws" / "modules" / "aquifer" / "main.tf").write_text("# module\n")
    (repo / "pyproject.toml").write_text("[project]\nname = 'aquifer'\n")
    return repo


@pytest.fixture
def setup(tmp_path):
    repo = make_repo(tmp_path)
    fake_tf = FakeTerraform(repo)
    aws = FakeAws("eu-west-1")
    tools = {"terraform", "aws", "session-manager-plugin"}
    tunnels: list[FakePopen] = []
    opened: list[str] = []
    ports = {"open": False}

    def popen(cmd, **kw):  # noqa: ANN001, ANN003, ANN202
        p = FakePopen(cmd, **kw)
        tunnels.append(p)
        ports["open"] = True
        return p

    system = System(run=fake_tf.run, stream=fake_tf.stream, which=lambda t: t in tools and t,
                    http_ok=lambda url, timeout=8.0: True, port_open=lambda port: ports["open"],
                    popen=popen, open_browser=opened.append, aws=lambda region: aws,
                    sleep=lambda s: None, monotonic=iter(range(0, 100000, 5)).__next__,
                    resolve=lambda host: None)
    return {"repo": repo, "tf": fake_tf, "aws": aws, "tools": tools, "system": system,
            "tunnels": tunnels, "opened": opened, "ports": ports}


def run(setup, **opts) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    defaults = {"region": "eu-west-1", "yes": True, "source_dir": str(setup["repo"])}
    o = Options(**{**defaults, **opts})
    code = Installer(o, Console(out=out, err=err, interactive=False), setup["system"]).run()
    return code, out.getvalue(), err.getvalue()


def install_state(setup) -> dict:
    return json.loads((setup["repo"] / "deploy/aws/.build/install-state.json").read_text())


# --- installer: fresh, existing, missing prerequisites, recovery ---------------------------------

def test_fresh_install_reaches_the_ui_with_zero_sources(setup):
    code, out, err = run(setup)
    assert code == 0, err
    tfvars = tf.read_tfvars(setup["repo"] / "deploy/aws/terraform.tfvars")
    assert tfvars["repos"] == [] and tfvars["name"] == "aquifer"
    assert tfvars["instance_type"] == "t4g.small" and tfvars["domain"] == ""
    assert tfvars["extraction_model_id"] == "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
    # Plan first, then apply exactly that plan file.
    plan_cmd = setup["tf"].commands("plan")[0]
    apply_cmd = setup["tf"].commands("apply")[0]
    assert apply_cmd[-1].endswith(".build/plan.tfplan")
    assert any(a.startswith("-out=") and a.endswith(".build/plan.tfplan") for a in plan_cmd)
    assert not (setup["repo"] / "deploy/aws/.build/plan.tfplan").exists()  # applied once
    # Health checked on the server, then the tunnel opened and the browser pointed at it.
    assert any("healthz" in c for c in setup["aws"].remote)
    assert setup["tunnels"][0].cmd[:3] == ["aws", "ssm", "start-session"]
    assert "AWS-StartPortForwardingSession" in setup["tunnels"][0].cmd
    assert setup["opened"] == ["http://127.0.0.1:18080/"]
    assert "UI_URL=http://127.0.0.1:18080/" in out
    assert "MCP_TOKENS_REF=ssm:/aquifer/aquifer/mcp-tokens" in out
    # What the user is told: the account, state location, cost, how to reopen the tunnel.
    for text in ("111111111111", "terraform.tfstate", "~$19.16/month", "aquifer-aws connect",
                 "Sources → Add a source", "one trust domain", "none yet"):
        assert text in err, text
    assert SECRET not in out + err
    assert install_state(setup)["phase"] == "open"
    # A fresh install never rolls out over SSM (the first boot installs the package).
    assert not any("aquifer-update" in c for c in setup["aws"].remote)


def test_missing_tools_and_credentials_are_explained_and_nothing_is_created(setup):
    setup["tools"].clear()
    setup["aws"].fail_identity = True
    code, _, err = run(setup)
    assert code == 2
    assert "Terraform is not installed" in err and "developer.hashicorp.com" in err
    assert "session-manager-plugin is missing" in err
    assert "AWS credentials are missing or expired" in err and "aws sso login" in err
    assert "Nothing was created" in err
    assert not setup["tf"].commands("plan") and not setup["tf"].commands("apply")


def test_missing_bedrock_embedding_access_blocks_with_the_fix(setup):
    setup["aws"].runtime = FakeRuntime(deny_embedding=True)
    code, _, err = run(setup, check_only=True)
    assert code == 2
    assert "amazon.titan-embed-text-v2:0 cannot be invoked" in err
    assert not setup["tf"].commands("plan")


def test_check_only_stops_before_configuration(setup):
    code, _, err = run(setup, check_only=True)
    assert code == 0 and "[1/5]" in err and "[2/5]" not in err
    assert not (setup["repo"] / "deploy/aws/terraform.tfvars").exists()


def test_existing_deployment_is_upgraded_in_place_and_rolled_out(setup):
    tfvars = setup["repo"] / "deploy/aws/terraform.tfvars"
    tfvars.write_text('# mine\nname = "aquifer"\nregion = "eu-west-1"\n'
                      'repos = ["acme/payments"]\ninstance_type = "t4g.medium"\n'
                      "data_volume_gb = 30\nextra_env = {}\n")
    (setup["repo"] / "deploy/aws/terraform.tfstate").write_text("{}")
    setup["tf"].state = ["module.aquifer.aws_instance.server", "module.aquifer.aws_ebs_volume.data"]
    setup["tf"].repos = ["acme/payments"]
    setup["aws"].params.add("/aquifer/aquifer/github-token")
    setup["tf"].plan = {"resource_changes": [
        {"address": "module.aquifer.aws_s3_object.wheel", "change": {"actions": ["update"]}},
        {"address": "module.aquifer.aws_ebs_volume.data", "previous_address": "aws_ebs_volume.data",
         "change": {"actions": ["no-op"]}}]}
    code, _, err = run(setup, extraction_model="none")
    assert code == 0, err
    assert "Existing deployment found" in err and "1 resources re-addressed" in err
    kept = tf.read_tfvars(tfvars)
    assert kept["repos"] == ["acme/payments"] and kept["instance_type"] == "t4g.medium"
    assert kept["data_volume_gb"] == 30  # never shrunk
    assert "# mine" in tfvars.read_text() and "extra_env = {}" in tfvars.read_text()
    assert any("aquifer-update" in c for c in setup["aws"].remote)
    assert not any(a.startswith("-migrate-state") for c in setup["tf"].calls for a in c)


def test_rerun_without_changes_just_reopens(setup):
    setup["tf"].state = ["module.aquifer.aws_instance.server"]
    (setup["repo"] / "deploy/aws/terraform.tfstate").write_text("{}")
    setup["tf"].plan = {"resource_changes": [
        {"address": "module.aquifer.aws_instance.server", "change": {"actions": ["no-op"]}}]}
    code, out, err = run(setup)
    assert code == 0, err
    assert "No changes" in err and not setup["tf"].commands("apply")
    assert "UI_URL=" in out


def test_failed_apply_is_recorded_and_the_rerun_resumes(setup):
    setup["tf"].apply_rc = 1
    setup["tf"].apply_text = ("module.aquifer.aws_vpc.this: Creation complete after 2s\n"
                              "Error: creating EC2 Instance: InsufficientInstanceCapacity\n")
    code, _, err = run(setup)
    assert code == 2
    assert "stopped part-way" in err and "InsufficientInstanceCapacity" in err
    assert "Resume with the same command" in err
    assert install_state(setup)["phase"] == "failed"
    assert not (setup["repo"] / "deploy/aws/.build/plan.tfplan").exists()  # never reapplied

    setup["tf"].apply_rc = 0
    setup["tf"].apply_text = "module.aquifer.aws_instance.server: Creation complete after 9s\n"
    code, _, err = run(setup)
    assert code == 0, err
    assert "A previous installation stopped during 'failed'" in err
    assert len(setup["tf"].commands("plan")) == 2  # a fresh plan, not the stale one
    assert install_state(setup)["phase"] == "open"


def test_resume_adopts_leftovers_and_reports_untracked_duplicates(setup):
    # An interrupted run: some resources recorded, others created in AWS but not recorded.
    (setup["repo"] / "deploy/aws/terraform.tfstate").write_text("{}")
    setup["tf"].state = ["module.aquifer.aws_iam_role.server"]
    build = setup["repo"] / "deploy/aws/.build"
    build.mkdir(parents=True)
    (build / "install-state.json").write_text(json.dumps({"phase": "interrupted",
                                                          "name": "aquifer"}))
    tags = {"Project": "aquifer", "Environment": "aquifer", "ManagedBy": "terraform"}
    setup["aws"].owned[("iam_instance_profile", "aquifer-server")] = tags
    setup["aws"].tagged = ["arn:aws:dlm:eu-west-1:111111111111:policy/policy-0abc",
                           "arn:aws:ec2:eu-west-1:111111111111:snapshot/snap-1"]
    code, _, err = run(setup)
    assert code == 0, err
    imports = setup["tf"].commands("import")
    assert [c[-2:] for c in imports] == [["module.aquifer.aws_iam_instance_profile.server",
                                          "aquifer-server"]]
    assert "Adopted aquifer-server" in err and "Partial installation found" in err
    assert "aws dlm delete-lifecycle-policy --region eu-west-1 --policy-id policy-0abc" in err
    assert "snap-1" not in err  # snapshots are expected, not leftovers


def test_leftover_of_another_owner_is_not_adopted(setup):
    (setup["repo"] / "deploy/aws/terraform.tfstate").write_text("{}")
    setup["tf"].state = ["module.aquifer.aws_iam_role.server"]
    build = setup["repo"] / "deploy/aws/.build"
    build.mkdir(parents=True)
    (build / "install-state.json").write_text(json.dumps({"phase": "failed", "name": "aquifer"}))
    setup["aws"].owned[("log_group", "/aquifer/aquifer")] = {"Project": "other"}
    code, _, err = run(setup)
    assert code == 2 and "not tagged as part of deployment 'aquifer'" in err
    assert not setup["tf"].commands("import") and not setup["tf"].commands("apply")


def test_vpc_quota_is_checked_before_anything_is_created(setup):
    setup["aws"].vpcs = (5, 5)
    code, _, err = run(setup)
    assert code == 2 and "VPCs: 5 of 5 used in eu-west-1" in err and "Service Quotas" in err
    assert not setup["tf"].commands("plan")


def test_state_lock_error_explains_force_unlock(setup):
    setup["tf"].apply_rc = 1
    setup["tf"].apply_text = ("Error: Error acquiring the state lock\n\nLock Info:\n"
                              "  ID:        3f2a-lock\n  Path: x\n")
    code, _, err = run(setup)
    assert code == 2
    assert "locked by another run" in err and "force-unlock 3f2a-lock" in err


def test_plan_that_replaces_the_data_volume_is_refused(setup):
    setup["tf"].plan = {"resource_changes": [
        {"address": "module.aquifer.aws_ebs_volume.data",
         "change": {"actions": ["delete", "create"]}}]}
    code, _, err = run(setup)
    assert code == 2
    assert "REPLACES OR DESTROYS THE DATA VOLUME" in err and "--allow-data-replacement" in err
    assert not setup["tf"].commands("apply")


def test_non_interactive_without_yes_stops_after_the_plan(setup):
    code, _, err = run(setup, yes=False)
    assert code == 1
    assert setup["tf"].commands("plan") and not setup["tf"].commands("apply")
    assert "re-run with --yes" in err


def test_unmanaged_deployment_with_the_same_name_is_not_duplicated(setup):
    setup["aws"].roles.add("aquifer-server")
    code, _, err = run(setup)
    assert code == 2
    assert "already exists in this account but not in this Terraform state" in err
    assert not setup["tf"].commands("plan")


def test_state_of_another_deployment_is_not_renamed(setup):
    (setup["repo"] / "deploy/aws/terraform.tfstate").write_text("{}")
    setup["tf"].state = ["module.aquifer.aws_instance.server"]
    setup["tf"].name = "prod"
    code, _, err = run(setup, name="aquifer")
    assert code == 2 and "manages deployment 'prod'" in err
    assert not setup["tf"].commands("plan")


def test_s3_backend_is_written_and_local_state_migrated(setup):
    (setup["repo"] / "deploy/aws/terraform.tfstate").write_text("{}")
    setup["tf"].state = ["module.aquifer.aws_instance.server"]
    code, _, err = run(setup, backend="s3", state_bucket="team-tf-state",
                       state_lock="dynamodb:tf-locks")
    assert code == 0, err
    backend = (setup["repo"] / "deploy/aws/backend.tf").read_text()
    assert 'bucket         = "team-tf-state"' in backend
    assert 'key            = "aquifer/aquifer/terraform.tfstate"' in backend
    assert 'dynamodb_table = "tf-locks"' in backend
    init = setup["tf"].commands("init")[0]
    assert "-migrate-state" in init and "-force-copy" in init
    assert "s3://team-tf-state/aquifer/aquifer/terraform.tfstate" in err


def test_unreachable_state_bucket_is_a_blocker(setup):
    code, _, err = run(setup, backend="s3", state_bucket="nope")
    assert code == 2 and "State bucket nope is not reachable" in err
    assert not (setup["repo"] / "deploy/aws/backend.tf").exists()


def test_https_access_falls_back_to_the_tunnel_until_dns_points_at_the_server(setup):
    code, out, err = run(setup, access="https", domain="aquifer.example.com")
    assert code == 0, err
    assert tf.read_tfvars(setup["repo"] / "deploy/aws/terraform.tfvars")["domain"] == \
        "aquifer.example.com"
    assert "Create a DNS A record aquifer.example.com → 203.0.113.10" in err
    assert setup["tunnels"] and "UI_URL=http://127.0.0.1:18080/" in out


def test_https_requires_a_domain(setup):
    code, _, err = run(setup, access="https")
    assert code == 2 and "--access https needs --domain" in err


def test_source_checkout_is_required(setup, tmp_path):
    code, _, err = run(setup, source_dir=str(tmp_path / "elsewhere"))
    assert code == 2 and "git clone https://github.com/Senora-dev/aquifer-ai.git" in err


# --- Terraform files and helpers -------------------------------------------------------------

def test_tfvars_update_keeps_other_lines(tmp_path):
    path = tmp_path / "terraform.tfvars"
    path.write_text('# note\nname = "a"\nrepos = ["x/y"]\n# domain = ""\n')
    tf.update_tfvars(path, {"name": "b", "domain": "d.example.com"}, "# header")
    text = path.read_text()
    assert "# note" in text and '# domain = ""' in text
    assert tf.read_tfvars(path) == {"name": "b", "repos": ["x/y"], "domain": "d.example.com"}


def test_backend_round_trip(tmp_path):
    for lock in ("lockfile", "dynamodb:tf-locks"):
        b = tf.Backend("s3", "bkt", "aquifer/x/terraform.tfstate", "eu-west-1", lock)
        (tmp_path / "backend.tf").write_text(b.render())
        parsed = tf.existing_backend(tmp_path)
        assert (parsed.bucket, parsed.key, parsed.region, parsed.lock) == \
            ("bkt", "aquifer/x/terraform.tfstate", "eu-west-1", lock)


def test_plan_review_flags_data_loss_and_moves():
    r = tf.review_plan({"resource_changes": [
        {"address": "module.aquifer.aws_ebs_volume.data", "change": {"actions": ["delete"]}},
        {"address": "module.aquifer.aws_instance.server", "previous_address": "aws_instance.server",
         "change": {"actions": ["create", "delete"]}},
        {"address": "module.aquifer.aws_vpc.this", "change": {"actions": ["no-op"]}}]})
    assert r.data_loss == ["module.aquifer.aws_ebs_volume.data"]
    assert r.disruptive == ["module.aquifer.aws_instance.server"]
    assert r.moved == 1 and r.changes == 2


def test_tainted_volume_of_an_interrupted_first_install_may_be_replaced():
    # Interrupted while creating the volume, before any server existed: it holds no data.
    changes = [
        {"address": "module.aquifer.aws_ebs_volume.data",
         "action_reason": "replace_because_tainted",
         "change": {"actions": ["delete", "create"]}},
        {"address": "module.aquifer.aws_instance.server", "change": {"actions": ["create"]}},
        {"address": "module.aquifer.aws_volume_attachment.data", "change": {"actions": ["create"]}}]
    r = tf.review_plan({"resource_changes": changes})
    assert r.tainted == ["module.aquifer.aws_ebs_volume.data"] and r.data_loss == []
    # Once a server exists, a tainted volume may hold the index: still refused.
    changes[1]["change"]["actions"] = ["no-op"]
    assert tf.review_plan({"resource_changes": changes}).data_loss == [
        "module.aquifer.aws_ebs_volume.data"]
    # Replacing a volume for any other reason is refused too.
    del changes[0]["action_reason"]
    changes[1]["change"]["actions"] = ["create"]
    assert tf.review_plan({"resource_changes": changes}).data_loss


def test_sizes_match_the_operations_script():
    script = (ROOT / "deploy/aws/aquifer-aws").read_text()
    for size, (instance, gb, usd, _) in tf.SIZES.items():
        assert re.search(rf'{size}\)\s+echo "{instance} {gb} {usd:.2f}"', script), size


def test_every_module_resource_has_a_moved_block():
    module = (ROOT / "deploy/aws/modules/aquifer/main.tf").read_text()
    resources = re.findall(r'^resource "(\w+)" "(\w+)"', module, re.M)
    root = (ROOT / "deploy/aws/main.tf").read_text()
    moved = set(re.findall(r"from = (\S+)\n\s+to\s+= module\.aquifer\.(\S+)", root))
    assert len(resources) == 23
    for rtype, name in resources:
        assert (f"{rtype}.{name}", f"{rtype}.{name}") in moved, f"{rtype}.{name}"


def test_root_module_exposes_the_documented_outputs():
    outputs = (ROOT / "deploy/aws/outputs.tf").read_text()
    module_outputs = (ROOT / "deploy/aws/modules/aquifer/outputs.tf").read_text()
    for name in ("instance_id", "ui_url", "mcp_url", "mcp_tokens_ref", "ssm_prefix", "repos",
                 "public_ip", "log_group"):
        assert f'output "{name}"' in outputs and f'output "{name}"' in module_outputs


def test_cli_help_lists_the_install_command():
    proc = subprocess.run([sys.executable, "-m", "aquifer.node.cli", "install", "aws", "--help"],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    for flag in ("--region", "--size", "--access", "--backend", "--state-bucket", "--yes",
                 "--check-only", "--plan-only", "--allow-data-replacement"):
        assert flag in proc.stdout


def test_documented_commands_exist():
    """Every `aquifer install aws` flag and `aquifer-aws` subcommand in the docs is real."""
    import argparse

    from aquifer.install.aws import add_parser

    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers())
    flags = {a for action in parser._subparsers._group_actions[0].choices["aws"]._actions
             for a in action.option_strings}
    script = (ROOT / "deploy/aws/aquifer-aws").read_text()
    commands = set(re.findall(r"^\s+([a-z][a-z-]*|_[a-z-]+)\)\s", script, re.M))
    docs = ["README.md", "AGENTS.md", "docs/install-aws.md", "docs/agent-integration.md",
            "docs/ui.md", "docs/deployment.md", "docs/getting-started.md", "deploy/aws/README.md"]
    for doc in docs:
        text = (ROOT / doc).read_text()
        for line in re.findall(r"aquifer install aws([^\n`|]*)", text):
            for flag in re.findall(r"(--[a-z-]+)", line):
                assert flag in flags, f"{doc}: aquifer install aws {flag}"
        for sub in re.findall(r"aquifer-aws ([a-z][a-z-]+)", text):
            assert sub in commands, f"{doc}: aquifer-aws {sub}"
