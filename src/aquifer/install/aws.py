"""``aquifer install aws`` — guided installation of Aquifer on AWS with Terraform.

The steps: check prerequisites, configure, prepare state and review the plan, deploy, open the
product. It drives the root module in ``deploy/aws`` (the reusable module is
``deploy/aws/modules/aquifer``) with the same ``terraform.tfvars``, ``.build/`` directory and
state as ``deploy/aws/aquifer-aws``, so the operational commands keep working afterwards.

Re-running is safe: Terraform converges on the recorded state, so an interrupted installation
resumes where it stopped without creating anything twice. Secrets are never printed, passed as
arguments or written to Terraform state.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aquifer.install import terraform as tf
from aquifer.install.bedrock import EMBEDDING_MODEL

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{2,27}$")
DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
MIN_TERRAFORM = (1, 6)
MIN_TERRAFORM_LOCKFILE = (1, 10)
REMOTE_PORT = 8080
REPO_URL = "https://github.com/Senora-dev/aquifer-ai.git"
TFVARS_HEADER = ("# Written by `aquifer install aws` / ./aquifer-aws configure — edit freely, "
                 "then re-run `aquifer install aws` (or ./aquifer-aws deploy).")

# What `terraform apply` needs. Checked with IAM policy simulation where the caller may run it.
REQUIRED_ACTIONS = [
    "ec2:CreateVpc", "ec2:CreateSubnet", "ec2:CreateSecurityGroup", "ec2:RunInstances",
    "ec2:CreateVolume", "ec2:AttachVolume", "ec2:AllocateAddress", "ec2:AssociateAddress",
    "iam:CreateRole", "iam:PutRolePolicy", "iam:AttachRolePolicy", "iam:CreateInstanceProfile",
    "iam:PassRole", "s3:CreateBucket", "s3:PutObject", "ssm:PutParameter", "ssm:GetParameter",
    "ssm:SendCommand", "ssm:StartSession", "logs:CreateLogGroup", "dlm:CreateLifecyclePolicy",
    "bedrock:InvokeModel",
]


class InstallError(Exception):
    """A blocker with what to do about it."""

    def __init__(self, title: str, fix: str = "") -> None:
        super().__init__(title)
        self.title = title
        self.fix = fix


# --- console -----------------------------------------------------------------------------

class Console:
    """Human output on stderr; ``KEY=value`` lines for scripts and agents on stdout."""

    def __init__(self, out=None, err=None, interactive: bool = False,  # noqa: ANN001
                 ask=None) -> None:  # noqa: ANN001
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        self.interactive = interactive
        self._ask = ask or input
        color = hasattr(self.err, "isatty") and self.err.isatty() and not os.environ.get("NO_COLOR")
        self._c = (lambda code, s: f"\033[{code}m{s}\033[0m") if color else (lambda _c, s: s)

    def _p(self, text: str = "") -> None:
        print(text, file=self.err, flush=True)

    def step(self, n: int, title: str) -> None:
        self._p()
        self._p(self._c("1", f"[{n}/5] {title}"))

    def ok(self, text: str) -> None:
        self._p(f"  {self._c('32', '✓')} {text}")

    def warn(self, text: str) -> None:
        self._p(f"  {self._c('33', '!')} {text}")

    def fail(self, text: str, fix: str = "") -> None:
        self._p(f"  {self._c('31', '✗')} {text}")
        for line in fix.splitlines():
            self._p(f"      {line}")

    def info(self, text: str = "") -> None:
        self._p(f"    {text}" if text else "")

    def kv(self, key: str, value: str) -> None:
        print(f"{key}={value}", file=self.out, flush=True)

    def ask(self, question: str, default: str, choices: list[str] | None = None) -> str:
        if not self.interactive:
            return default
        hint = f" [{'/'.join(choices)}]" if choices else ""
        while True:
            answer = self._ask(f"  {question}{hint} (default {default or 'none'}): ").strip()
            answer = answer or default
            if not choices or answer in choices:
                return answer
            self._p(f"    choose one of: {', '.join(choices)}")

    def confirm(self, question: str) -> bool:
        if not self.interactive:
            return False
        return self._ask(f"  {question} [y/N] ").strip().lower() in {"y", "yes"}


# --- AWS (boto3) -----------------------------------------------------------------------------

class AwsApi:
    """The AWS calls the installer makes, behind one seam for tests."""

    def __init__(self, region: str, session=None) -> None:  # noqa: ANN001
        import boto3

        self.region = region
        self.session = session or boto3.Session(region_name=region)

    def client(self, name: str, region: str | None = None):  # noqa: ANN201
        return self.session.client(name, region_name=region or self.region)

    def identity(self) -> dict:
        return self.client("sts").get_caller_identity()

    def region_status(self) -> str:
        regions = self.client("ec2").describe_regions(AllRegions=True)["Regions"]
        for r in regions:
            if r["RegionName"] == self.region:
                return r.get("OptInStatus", "")
        return "unknown"

    def instance_type_offered(self, instance_type: str) -> bool:
        resp = self.client("ec2").describe_instance_type_offerings(
            LocationType="region", Filters=[{"Name": "instance-type", "Values": [instance_type]}])
        return bool(resp["InstanceTypeOfferings"])

    def simulation_arn(self, arn: str) -> str:
        """IAM policy simulation needs the role, not the assumed-role session."""
        m = re.match(r"arn:([\w-]+):sts::(\d+):assumed-role/([^/]+)/", arn)
        if not m:
            return arn
        return self.client("iam").get_role(RoleName=m.group(3))["Role"]["Arn"]

    def denied_actions(self, arn: str, actions: list[str]) -> list[str]:
        results = self.client("iam").simulate_principal_policy(
            PolicySourceArn=self.simulation_arn(arn), ActionNames=actions)["EvaluationResults"]
        return [r["EvalActionName"] for r in results if r["EvalDecision"] != "allowed"]

    def role_exists(self, name: str) -> bool:
        iam = self.client("iam")
        try:
            iam.get_role(RoleName=name)
            return True
        except iam.exceptions.NoSuchEntityException:
            return False

    def parameter_exists(self, name: str) -> bool:
        resp = self.client("ssm").describe_parameters(
            ParameterFilters=[{"Key": "Name", "Option": "Equals", "Values": [name]}])
        return bool(resp["Parameters"])

    def bedrock(self):  # noqa: ANN201
        return self.client("bedrock"), self.client("bedrock-runtime")

    def _quota(self, service: str, code: str, default: float) -> float:
        try:
            return self.client("service-quotas").get_service_quota(
                ServiceCode=service, QuotaCode=code)["Quota"]["Value"]
        except Exception:  # noqa: BLE001 - not every caller may read quotas
            return default

    def vpc_headroom(self) -> tuple[int, int]:
        used = len(self.client("ec2").describe_vpcs()["Vpcs"])
        return used, int(self._quota("vpc", "L-F678F1CE", 5))

    def eip_headroom(self) -> tuple[int, int]:
        used = len(self.client("ec2").describe_addresses()["Addresses"])
        return used, int(self._quota("ec2", "L-0263D0A3", 5))

    def tags_of(self, kind: str, name: str) -> dict | None:
        """Tags of a named resource, or None if it does not exist."""
        try:
            if kind == "iam_role":
                return {t["Key"]: t["Value"] for t in
                        self.client("iam").get_role(RoleName=name)["Role"].get("Tags", [])}
            if kind == "iam_instance_profile":
                prof = self.client("iam").get_instance_profile(InstanceProfileName=name)
                return {t["Key"]: t["Value"] for t in prof["InstanceProfile"].get("Tags", [])}
            if kind == "ssm_parameter":
                if not self.parameter_exists(name):
                    return None
                tags = self.client("ssm").list_tags_for_resource(
                    ResourceType="Parameter", ResourceId=name)["TagList"]
                return {t["Key"]: t["Value"] for t in tags}
            if kind == "log_group":
                logs = self.client("logs")
                groups = logs.describe_log_groups(logGroupNamePrefix=name)["logGroups"]
                if not any(g["logGroupName"] == name for g in groups):
                    return None
                return logs.list_tags_log_group(logGroupName=name).get("tags", {})
        except Exception as exc:  # noqa: BLE001
            if "NoSuchEntity" in type(exc).__name__ or "NoSuchEntity" in str(exc):
                return None
            raise
        raise ValueError(kind)

    def role_policy_exists(self, role: str, policy: str) -> bool:
        try:
            self.client("iam").get_role_policy(RoleName=role, PolicyName=policy)
            return True
        except Exception:  # noqa: BLE001
            return False

    def tagged_resources(self, name: str) -> list[str]:
        """ARNs tagged for this deployment (Project=aquifer, Environment=<name>)."""
        out: list[str] = []
        pages = self.client("resourcegroupstaggingapi").get_paginator("get_resources").paginate(
            TagFilters=[{"Key": "Project", "Values": ["aquifer"]},
                        {"Key": "Environment", "Values": [name]}])
        for page in pages:
            out += [m["ResourceARN"] for m in page["ResourceTagMappingList"]]
        return out

    def bucket_status(self, bucket: str, region: str) -> tuple[bool, bool]:
        """(reachable, versioning enabled)."""
        s3 = self.client("s3", region)
        try:
            s3.head_bucket(Bucket=bucket)
        except Exception:  # noqa: BLE001 - 403/404 are both "cannot use it"
            return False, False
        status = s3.get_bucket_versioning(Bucket=bucket).get("Status")
        return True, status == "Enabled"

    def lock_table_exists(self, table: str, region: str) -> bool:
        ddb = self.client("dynamodb", region)
        try:
            ddb.describe_table(TableName=table)
            return True
        except Exception:  # noqa: BLE001
            return False

    def instance_online(self, instance_id: str) -> bool:
        resp = self.client("ssm").describe_instance_information(
            Filters=[{"Key": "InstanceIds", "Values": [instance_id]}])
        return any(i.get("PingStatus") == "Online" for i in resp["InstanceInformationList"])

    def run_remote(self, instance_id: str, command: str, timeout_s: int = 900) -> tuple[str, str]:
        """Run a shell command on the server (SSM Run Command). Returns (status, output)."""
        ssm = self.client("ssm")
        cmd_id = ssm.send_command(
            InstanceIds=[instance_id], DocumentName="AWS-RunShellScript",
            Parameters={"commands": [command], "executionTimeout": [str(timeout_s)]},
        )["Command"]["CommandId"]
        deadline = time.monotonic() + timeout_s + 60
        while True:
            time.sleep(2)
            try:
                inv = ssm.get_command_invocation(CommandId=cmd_id, InstanceId=instance_id)
            except ssm.exceptions.InvocationDoesNotExist:
                continue
            if inv["Status"] not in {"Pending", "InProgress", "Delayed"}:
                out = inv.get("StandardOutputContent", "") + inv.get("StandardErrorContent", "")
                return inv["Status"], out
            if time.monotonic() > deadline:
                return "TimedOut", ""


# --- system seams ------------------------------------------------------------------------------

def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", text)[:3])


def http_ok(url: str, timeout: float = 8.0) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - fixed URLs
            return 200 <= resp.status < 400
    except Exception:  # noqa: BLE001
        return False


def port_open(port: int) -> bool:
    import socket

    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


@dataclass
class System:
    """Everything outside this process, replaceable in tests."""

    run: Any = None  # (cmd, cwd, env) -> CompletedProcess (captured)
    stream: Any = None  # (cmd, cwd, env, on_line) -> (returncode, text)
    which: Any = shutil.which
    http_ok: Any = http_ok
    port_open: Any = port_open
    popen: Any = subprocess.Popen
    open_browser: Any = None
    aws: Any = AwsApi  # (region) -> AwsApi
    sleep: Any = time.sleep
    monotonic: Any = time.monotonic
    resolve: Any = None  # hostname -> ip

    def __post_init__(self) -> None:
        if self.run is None:
            self.run = lambda cmd, cwd=None, env=None: subprocess.run(
                cmd, cwd=cwd, env=env, capture_output=True, text=True, check=False)
        if self.stream is None:
            self.stream = _stream
        if self.open_browser is None:
            import webbrowser

            self.open_browser = webbrowser.open
        if self.resolve is None:
            import socket

            def resolve(host: str) -> str | None:
                try:
                    return socket.gethostbyname(host)
                except OSError:
                    return None

            self.resolve = resolve


def _stream(cmd, cwd=None, env=None, on_line=None) -> tuple[int, str]:  # noqa: ANN001
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    lines = []
    assert proc.stdout is not None
    for line in proc.stdout:
        lines.append(line)
        if on_line:
            on_line(line.rstrip("\n"))
    return proc.wait(), "".join(lines)


# --- options -----------------------------------------------------------------------------------

@dataclass
class Options:
    region: str | None = None
    name: str | None = None
    size: str | None = None
    access: str | None = None
    domain: str | None = None
    https_cidrs: list[str] = field(default_factory=list)
    extraction_model: str | None = None  # auto | none | <model id>
    backend: str | None = None  # local | s3
    state_bucket: str | None = None
    state_key: str | None = None
    state_region: str | None = None
    state_lock: str | None = None  # lockfile | dynamodb:<table>
    local_port: int = 18080
    source_dir: str | None = None
    yes: bool = False
    check_only: bool = False
    plan_only: bool = False
    no_browser: bool = False
    allow_data_replacement: bool = False
    interactive: bool = False


@dataclass
class Settings:
    """The deployment settings shown to the user and written to terraform.tfvars."""

    name: str
    region: str
    size: str
    access: str
    domain: str
    https_cidrs: list[str]
    extraction_model: str
    backend: tf.Backend
    repos: list[str] = field(default_factory=list)
    data_volume_gb: int = 10


# --- the installer -----------------------------------------------------------------------------

class Installer:
    def __init__(self, opts: Options, console: Console, system: System | None = None) -> None:
        self.o = opts
        self.c = console
        self.sys = system or System()
        self.aws: AwsApi | None = None
        self.identity: dict = {}
        self.extraction_model = ""
        self.existing = False  # this state already manages a deployment
        self.resuming = False  # ... left behind by an interrupted or failed run
        self.prev: dict = {}  # the previous run's record (.build/install-state.json)
        self.settings: Settings | None = None
        self.review: tf.PlanReview | None = None
        self.repo_root = Path()
        self.tfdir = Path()

    # paths and state --------------------------------------------------------------------

    @property
    def build(self) -> Path:
        return self.tfdir / ".build"

    @property
    def state_file(self) -> Path:
        return self.build / "install-state.json"

    def _record(self, phase: str, error: str = "") -> None:
        self.build.mkdir(parents=True, exist_ok=True)
        s = self.settings
        self.state_file.write_text(json.dumps({
            "phase": phase, "error": error, "name": s.name if s else None,
            "region": s.region if s else self.o.region,
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds")}, indent=2))

    def previous_run(self) -> dict | None:
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return None

    def _tf(self, *args: str) -> list[str]:
        return ["terraform", f"-chdir={self.tfdir}", *args]

    def _env(self) -> dict:
        region = self.settings.region if self.settings else (self.o.region or "")
        return {**os.environ, "AWS_REGION": region, "AWS_DEFAULT_REGION": region,
                "TF_IN_AUTOMATION": "1", "AWS_PAGER": ""}

    def _run(self, cmd: list[str]):  # noqa: ANN202
        return self.sys.run(cmd, cwd=str(self.tfdir), env=self._env())

    # entry ------------------------------------------------------------------------------

    def run(self) -> int:
        try:
            self.locate_source()
            prev = self.prev = self.previous_run() or {}
            if prev and prev.get("phase") not in {None, "open"}:
                self.c.warn(f"A previous installation stopped during '{prev['phase']}' "
                            f"({prev.get('updated_at')}): resuming. Terraform state records "
                            "what exists, so nothing is created twice.")
            self.check_prerequisites()
            if self.o.check_only:
                return 0
            self.configure()
            self._record("configured")
            self.prepare_and_review()
            if self.o.plan_only:
                self.c.info("Plan only: nothing was changed. Run again without --plan-only to "
                            "deploy (a new plan is made and shown first).")
                return 0
            if not self.approve():
                return 1
            self.deploy()
            self.open_product()
            self._record("open")
            return 0
        except InstallError as e:
            self.c.fail(e.title, e.fix)
            if self.settings is not None:
                self._record("failed", e.title)
            return 2
        except KeyboardInterrupt:
            self.c.fail("Interrupted.", self.resume_hint())
            if self.settings is not None:
                self._record("interrupted")
            return 130

    def resume_hint(self) -> str:
        return ("Resume with the same command: `aquifer install aws` (add the same flags). "
                "Terraform state records what was created; the next plan shows only what "
                "is left.")

    # source checkout ----------------------------------------------------------------------

    def locate_source(self) -> None:
        import aquifer

        root = Path(self.o.source_dir).expanduser().resolve() if self.o.source_dir else \
            Path(aquifer.__file__).resolve().parents[2]
        if not (root / "deploy" / "aws" / "modules" / "aquifer" / "main.tf").is_file() or \
                not (root / "pyproject.toml").is_file():
            raise InstallError(
                "The installer needs the Aquifer source checkout (Terraform module and server "
                "package), and this `aquifer` was not installed from one.",
                f"git clone {REPO_URL} && cd aquifer-ai\n"
                "python3 -m venv .venv && .venv/bin/pip install -e \".[server]\"\n"
                ".venv/bin/aquifer install aws        (or pass --source-dir PATH)")
        self.repo_root = root
        self.tfdir = root / "deploy" / "aws"

    # 1. prerequisites -----------------------------------------------------------------------

    def check_prerequisites(self) -> None:
        self.c.step(1, "Check prerequisites")
        blockers: list[InstallError] = []

        def block(title: str, fix: str) -> None:
            self.c.fail(title, fix)
            blockers.append(InstallError(title, fix))

        existing = tf.read_tfvars(self.tfdir / "terraform.tfvars")
        private = (self.o.access or ("https" if existing.get("domain") else "private")) == "private"

        tf_path = self.sys.which("terraform")
        if not tf_path:
            block("Terraform is not installed.",
                  "Install Terraform >= 1.6: https://developer.hashicorp.com/terraform/install"
                  "\n(macOS: brew tap hashicorp/tap && brew install hashicorp/tap/terraform)")
        else:
            out = self.sys.run(["terraform", "version", "-json"]).stdout or "{}"
            try:
                version = json.loads(out).get("terraform_version", "0")
            except ValueError:
                version = "0"
            needed = MIN_TERRAFORM_LOCKFILE if self.o.state_lock in (None, "lockfile") and \
                self.o.backend == "s3" else MIN_TERRAFORM
            if _version_tuple(version) < needed:
                block(f"Terraform {version} is too old; this needs >= "
                      f"{'.'.join(map(str, needed))}.",
                      "Upgrade: https://developer.hashicorp.com/terraform/install")
            else:
                self.c.ok(f"Terraform {version}")
        for tool, why, fix in (
            ("aws", "the SSM tunnel to the UI and the operations script",
             "Install AWS CLI v2: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"),
            ("session-manager-plugin", "the SSM tunnel (private access)",
             "Install the Session Manager plugin: https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html\n"
             "(macOS: brew install --cask session-manager-plugin)"),
        ):
            if self.sys.which(tool):
                self.c.ok(f"{tool}")
            elif private:
                block(f"{tool} is missing; it is needed for {why}.", fix)
            else:
                self.c.warn(f"{tool} missing: needed for {why} and operations "
                            f"({fix.splitlines()[0]})")
        pip = self.sys.run([sys.executable, "-m", "pip", "--version"])
        if pip.returncode != 0:
            block("pip is not available for this Python; it builds the server package.",
                  f"{sys.executable} -m ensurepip --upgrade")
        try:
            import boto3  # noqa: F401
        except ImportError:
            block("boto3 is missing from this environment.",
                  "Install the CLI with its AWS dependencies: pip install -e \".[server]\"")
            raise self._blocked(blockers) from None

        region = self.o.region or existing.get("region") or ""
        if not region:
            region = self.c.ask("AWS region", "eu-west-1")
        self.o.region = region

        # AWS identity, region, permissions -------------------------------------------------
        try:
            self.aws = self.sys.aws(region)
            self.identity = self.aws.identity()
        except InstallError:
            raise
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "ProfileNotFound":
                block(f"AWS profile {os.environ.get('AWS_PROFILE', '')!r} does not exist.",
                      "List profiles: aws configure list-profiles. Then export AWS_PROFILE=<one "
                      "of them> (or unset AWS_PROFILE) and run this again.")
                raise self._blocked(blockers) from None
            block("AWS credentials are missing or expired "
                  f"({type(exc).__name__}).",
                  "Log in, then run this again: aws sso login (or aws configure, or export "
                  "AWS_PROFILE=<profile>).")
            raise self._blocked(blockers) from None
        self.c.ok(f"AWS account {self.identity['Account']} ({self.identity['Arn']})")
        try:
            status = self.aws.region_status()
        except Exception:  # noqa: BLE001
            status = "unknown"
        if status in {"opt-in-not-required", "opted-in"}:
            self.c.ok(f"Region {region}")
        elif status == "not-opted-in":
            block(f"Region {region} is not enabled for this account.",
                  "Enable it (AWS console → Account → AWS Regions), or pass --region.")
        else:
            self.c.warn(f"Could not confirm that region {region} is enabled ({status}).")
        size = self.o.size or tf.size_for_instance(existing.get("instance_type", "")) or "small"
        if size in tf.SIZES:
            itype = tf.SIZES[size][0]
            try:
                offered = self.aws.instance_type_offered(itype)
            except Exception:  # noqa: BLE001
                offered = True
            if offered:
                self.c.ok(f"{itype} is offered in {region}")
            else:
                block(f"{itype} is not offered in {region}.", "Choose another --region.")
        self._check_quotas(block)
        try:
            denied = self.aws.denied_actions(self.identity["Arn"], REQUIRED_ACTIONS)
        except Exception as exc:  # noqa: BLE001
            self.c.warn(f"Permissions not verified (policy simulation unavailable: "
                        f"{type(exc).__name__}). Terraform reports missing permissions at plan "
                        "or apply time.")
        else:
            if denied:
                block("Your AWS identity is not allowed: " + ", ".join(denied),
                      "Use a role that can create VPC/EC2, IAM roles, S3, SSM parameters, "
                      "CloudWatch Logs and DLM policies (e.g. AdministratorAccess in a sandbox "
                      "account). Simulation ignores SCPs and permission boundaries.")
            else:
                self.c.ok("IAM permissions for the deployment (policy simulation; SCPs and "
                          "permission boundaries are not evaluated)")

        # connectivity ----------------------------------------------------------------------
        if (self.tfdir / ".terraform" / "providers").is_dir():
            self.c.ok("Terraform providers already installed")
        elif self.sys.http_ok("https://registry.terraform.io/.well-known/terraform.json"):
            self.c.ok("registry.terraform.io reachable (provider download)")
        else:
            block("Cannot reach registry.terraform.io to download the AWS provider.",
                  "Check the network or proxy (HTTPS_PROXY), or configure a provider mirror.")
        if self.sys.http_ok("https://pypi.org/simple/hatchling/"):
            self.c.ok("pypi.org reachable (builds the server package)")
        else:
            block("Cannot reach pypi.org to build the server package.",
                  "Check the network or proxy, or set PIP_INDEX_URL to your package mirror.")

        # Bedrock ------------------------------------------------------------------------------
        self._check_bedrock(block, existing)
        if blockers:
            raise self._blocked(blockers)

    def _check_quotas(self, block) -> None:  # noqa: ANN001
        """A new deployment needs one VPC and one Elastic IP in the region."""
        state = self._state_resources()
        for address, label, headroom, fix in (
            ("aws_vpc.this", "VPCs", self.aws.vpc_headroom,
             "Delete an unused VPC, request a higher 'VPCs per Region' quota (Service Quotas → "
             "Amazon VPC), or choose another --region."),
            ("aws_eip.server", "Elastic IPs", self.aws.eip_headroom,
             "Release an unused Elastic IP, request a higher 'EC2-VPC Elastic IPs' quota "
             "(Service Quotas → Amazon EC2), or choose another --region."),
        ):
            if any(a.endswith(address) for a in state):
                continue  # this deployment already has it
            try:
                used, quota = headroom()
            except Exception:  # noqa: BLE001
                self.c.warn(f"Could not check the {label} quota; apply reports it if exceeded.")
                continue
            if used >= quota:
                block(f"{label}: {used} of {quota} used in {self.o.region}; this deployment "
                      "needs one more.", fix)
            else:
                self.c.ok(f"{label}: {used} of {quota} used; one more is needed")

    def _blocked(self, blockers: list[InstallError]) -> InstallError:
        return InstallError(f"{len(blockers)} prerequisite(s) missing (see above). Nothing was "
                            "created.", "Fix them and run the same command again.")

    def _check_bedrock(self, block, existing: dict) -> None:  # noqa: ANN001
        from aquifer.install import bedrock

        control, runtime = self.aws.bedrock()
        r = bedrock.check_embedding(runtime)
        if r.ok:
            self.c.ok(f"Bedrock embeddings {EMBEDDING_MODEL} (required)")
        else:
            block(f"Bedrock embeddings {EMBEDDING_MODEL} cannot be invoked: {r.code}", r.message)
        wanted = self.o.extraction_model
        if wanted is None and "extraction_model_id" in existing:
            wanted = existing["extraction_model_id"] or "none"
        wanted = wanted or "auto"
        if wanted == "none":
            self.extraction_model = ""
            self.c.warn("LLM extraction and grounded answers: off (--extraction-model none). "
                        "Deterministic extraction and source-backed search still work.")
        elif wanted == "auto":
            self.extraction_model = bedrock.detect_extraction_model(
                control, runtime, log=lambda m: self.c.info(m))
            if self.extraction_model:
                self.c.ok(f"Claude model {self.extraction_model} (LLM extraction and grounded "
                          "answers; auto-detected)")
            else:
                self.c.warn(f"No Claude model is invocable in {self.o.region}: LLM extraction "
                            "and grounded answers will be off. To enable them, request model "
                            "access in the Bedrock console, then re-run.")
        else:
            r = bedrock.check_chat(runtime, wanted)
            if r.ok:
                self.extraction_model = wanted
                self.c.ok(f"Claude model {wanted} (LLM extraction and grounded answers)")
            else:
                block(f"Bedrock model {wanted} cannot be invoked: {r.code}",
                      r.message + "\nOr pass --extraction-model auto|none.")

    # 2. configure ---------------------------------------------------------------------------

    def configure(self) -> None:
        self.c.step(2, "Configure the deployment")
        existing = tf.read_tfvars(self.tfdir / "terraform.tfvars")
        state = self._state_resources()
        self.existing = bool(state)
        prev = self.prev
        self.resuming = self.existing and prev.get("phase") in {
            "configured", "planned", "applying", "applied", "failed", "interrupted"}
        if self.resuming:
            self.c.ok(f"Partial installation found ({len(state)} resources already created): "
                      "resuming it.")
        elif self.existing:
            self.c.ok(f"Existing deployment found in this state ({len(state)} resources): "
                      "this run upgrades it in place.")
        default_name = existing.get("name") or "aquifer"
        name = self.o.name or (default_name if self.existing else
                               self.c.ask("Deployment name", default_name))
        if not NAME_RE.match(name):
            raise InstallError(f"Invalid name {name!r}.",
                               "3-28 lowercase letters, digits or hyphens, starting with a letter.")
        if self.existing and existing.get("name") and name != existing["name"]:
            raise InstallError(
                f"This directory's state manages deployment {existing['name']!r}, not {name!r}.",
                "One checkout (deploy/aws) manages one deployment. For another deployment use "
                "a second clone, or call the module from your own Terraform root "
                "(docs/install-aws.md#direct-terraform).")

        current_size = tf.size_for_instance(existing.get("instance_type", "")) or "small"
        if self.o.size:
            size = self.o.size
        else:
            if self.c.interactive:
                for k, (itype, gb, usd, fits) in tf.SIZES.items():
                    self.c.info(f"{k:<7} {itype:<11} {gb:>2} GiB data  ~${usd:.0f}/month  {fits}")
            size = self.c.ask("Server size", current_size, list(tf.SIZES))
        if size not in tf.SIZES:
            raise InstallError(f"Unknown size {size!r}.", "Use small, medium or large.")
        if size == "large" and not self.o.yes and not self.c.confirm(
                "large costs ~$62/month baseline, above the $50 budget. Use it?"):
            raise InstallError("Size large not confirmed.", "Choose --size small or medium.")

        domain = self.o.domain if self.o.domain is not None else existing.get("domain", "")
        access = self.o.access or ("https" if domain else
                                   self.c.ask("Access: private (SSM tunnel, no inbound ports) "
                                              "or https (public domain, bearer tokens)",
                                              "private", ["private", "https"]))
        if access == "https":
            if not domain:
                domain = self.c.ask("Domain for HTTPS (an A record you will point at the server)",
                                    "")
            if not DOMAIN_RE.match(domain or ""):
                raise InstallError("--access https needs --domain (a DNS name you control).",
                                   "Example: --access https --domain aquifer.example.com")
        else:
            domain = ""
        cidrs = self.o.https_cidrs or existing.get("https_allowed_cidrs") or ["0.0.0.0/0"]

        backend = self._choose_backend(name)
        gb = max(tf.SIZES[size][1], int(existing.get("data_volume_gb") or 0))  # never shrinks
        self.settings = Settings(
            name=name, region=self.o.region or "eu-west-1", size=size, access=access,
            domain=domain, https_cidrs=list(cidrs), extraction_model=self.extraction_model,
            backend=backend, repos=list(existing.get("repos") or []), data_volume_gb=gb)
        s = self.settings
        self.c.info()
        self.c.info(f"AWS account   {self.identity['Account']}")
        self.c.info(f"Region        {s.region}")
        self.c.info(f"Name          {s.name}  (resources and /aquifer/{s.name}/ in SSM)")
        self.c.info(f"Size          {s.size}: {tf.SIZES[s.size][0]}, {s.data_volume_gb} GiB data "
                    f"volume")
        if s.access == "private":
            self.c.info("Access        private: no inbound ports; the UI and MCP are reached "
                        "through an SSM tunnel from your machine")
        else:
            self.c.info(f"Access        https://{s.domain}/ (Let's Encrypt; allowed from "
                        f"{', '.join(s.https_cidrs)}); every request needs an access token")
        self.c.info(f"Claude model  {s.extraction_model or 'none (LLM extraction off)'}")
        sources = (", ".join(s.repos) + " (from terraform.tfvars)" if s.repos
                   else "none yet: connect them in the UI after installation")
        self.c.info(f"Sources       {sources}")
        self.c.info(f"State         {s.backend.describe()}")
        if self.o.region and existing.get("region") and self.o.region != existing["region"] \
                and self.existing:
            raise InstallError(
                f"The deployment is in {existing['region']}; it cannot move to {self.o.region}.",
                "Keep the region, or remove it (./aquifer-aws down) and install again.")

    def _state_resources(self) -> list[str]:
        if not (self.tfdir / ".terraform").is_dir() and \
                not (self.tfdir / "terraform.tfstate").is_file():
            return []
        r = self._run(self._tf("state", "list"))
        if r.returncode != 0:
            return []
        # Managed resources only (data sources are lookups, not things the deployment owns).
        return [x for x in r.stdout.splitlines()
                if x.strip() and not x.startswith("data.") and ".data." not in x]

    def _choose_backend(self, name: str) -> tf.Backend:
        current = tf.existing_backend(self.tfdir)
        if self.o.backend in (None, "") and current is not None:
            if current.kind != "s3":
                current.path = current.path or "see backend configuration"
            return current
        kind = self.o.backend or (
            "local" if self.existing else
            self.c.ask("Terraform state: local (a file in deploy/aws) or s3 (your team's "
                       "bucket, with locking)", "local", ["local", "s3"]))
        if kind == "local":
            if current is not None:
                raise InstallError(
                    "This checkout already uses a remote backend (backend.tf).",
                    "Moving state back to a local file is not automated: run `terraform "
                    "-chdir=deploy/aws init -migrate-state` after removing backend.tf.")
            return tf.Backend("local", path=str(self.tfdir / "terraform.tfstate"))
        if kind != "s3":
            raise InstallError(f"Unsupported backend {kind!r}.", "Use --backend local or s3.")
        bucket = self.o.state_bucket or self.c.ask("State bucket (existing S3 bucket)", "")
        if not bucket:
            raise InstallError("--backend s3 needs --state-bucket.",
                               "Pass the name of an existing, versioned S3 bucket.")
        b = tf.Backend("s3", bucket=bucket,
                       key=self.o.state_key or f"aquifer/{name}/terraform.tfstate",
                       region=self.o.state_region or self.o.region or "eu-west-1",
                       lock=self.o.state_lock or "lockfile")
        if not (b.lock == "lockfile" or re.match(r"^dynamodb:[\w.-]{3,255}$", b.lock)):
            raise InstallError(f"Invalid --state-lock {b.lock!r}.",
                               "Use lockfile (S3 native locking) or dynamodb:<table>.")
        reachable, versioned = self.aws.bucket_status(b.bucket, b.region)
        if not reachable:
            raise InstallError(f"State bucket {b.bucket} is not reachable in {b.region}.",
                               "Check the name, region and your s3:ListBucket/GetObject/"
                               "PutObject permissions. The installer does not create it.")
        if not versioned:
            self.c.warn(f"Bucket {b.bucket} has no versioning: enable it so a damaged state "
                        "can be recovered.")
        if b.lock.startswith("dynamodb:") and \
                not self.aws.lock_table_exists(b.lock.split(":", 1)[1], b.region):
            raise InstallError(f"Lock table {b.lock.split(':', 1)[1]} not found in {b.region}.",
                               "Create it (partition key LockID, string) or use --state-lock "
                               "lockfile.")
        return b

    # 3. state and preview ---------------------------------------------------------------------

    def prepare_and_review(self) -> None:
        self.c.step(3, "Prepare state and review the plan")
        s = self.settings
        self._write_tfvars()
        self._init_backend()
        state = self._state_resources()
        self.existing = bool(state)
        if self.existing:
            managed = self._state_name()
            if managed and managed != s.name:
                raise InstallError(
                    f"This state manages deployment {managed!r}, not {s.name!r}.",
                    f"Use --name {managed} to upgrade it. For another deployment use a second "
                    "clone or another state (docs/install-aws.md#direct-terraform).")
        elif not self._resuming_run():
            self._refuse_unmanaged_duplicate()
        wheel = self._build_wheel()
        if self._resuming_run():
            self._adopt_leftovers(wheel)
            self._report_untracked()
        plan = self.build / "plan.tfplan"
        self.c.ok("terraform plan (creates nothing)")
        rc, text = self.sys.stream(
            self._tf("plan", "-input=false", "-no-color", "-lock-timeout=60s", f"-out={plan}",
                     f"-var=wheel_path=.build/dist/{wheel}"),
            str(self.tfdir), self._env(), None)
        if rc != 0:
            raise self._terraform_error("terraform plan failed", text)
        shown = self._run(self._tf("show", "-json", str(plan)))
        self.review = tf.review_plan(json.loads(shown.stdout))
        self._show_plan()
        for line in tf.cost_lines(s.size, s.region, s.extraction_model, sources=bool(s.repos)):
            self.c.info(line)
        self._record("planned")

    def _write_tfvars(self) -> None:
        s = self.settings
        path = self.tfdir / "terraform.tfvars"
        values = {"name": s.name, "region": s.region, "extraction_model_id": s.extraction_model,
                  "instance_type": tf.SIZES[s.size][0], "data_volume_gb": s.data_volume_gb,
                  "domain": s.domain}
        if s.domain:
            values["https_allowed_cidrs"] = s.https_cidrs
        if not path.is_file():
            values["repos"] = []
        tf.update_tfvars(path, values, TFVARS_HEADER)
        self.c.ok(f"Settings written to {path.relative_to(self.repo_root)}")

    def _init_backend(self) -> None:
        s = self.settings
        backend_file = self.tfdir / tf.BACKEND_FILE
        args = ["init", "-input=false", "-no-color"]
        if s.backend.kind == "s3" and tf.existing_backend(self.tfdir) is None:
            had_local = self._state_resources()
            backend_file.write_text(s.backend.render())
            self.c.ok(f"Backend written to {backend_file.relative_to(self.repo_root)}")
            if had_local:
                if not (self.o.yes or self.c.confirm(
                        f"Copy the existing local state ({len(had_local)} resources) to "
                        f"s3://{s.backend.bucket}/{s.backend.key}?")):
                    backend_file.unlink()
                    raise InstallError("State migration not confirmed.",
                                       "Re-run and confirm, or keep --backend local.")
                args += ["-migrate-state", "-force-copy"]
        r = self._run(self._tf(*args))
        if r.returncode != 0:
            raise self._terraform_error("terraform init failed", r.stdout + r.stderr)
        self.c.ok(f"State: {s.backend.describe()}")

    def _resuming_run(self) -> bool:
        prev = self.prev
        return prev.get("phase") in {"applying", "applied", "failed", "interrupted"} and \
            prev.get("name") == self.settings.name

    def _owned(self, tags: dict | None) -> bool:
        return bool(tags) and tags.get("Project") == "aquifer" and \
            tags.get("Environment") == self.settings.name and tags.get("ManagedBy") == "terraform"

    def _adopt_leftovers(self, wheel: str) -> None:
        """Import resources an interrupted run created but did not record.

        AWS can finish a create after Terraform was interrupted; the resource then exists but
        is missing from state, and the next apply fails on its name. Only resources whose tags
        name this deployment are imported (the role policy has no tags: only with its role).
        """
        n = self.settings.name
        state = set(self._state_resources())
        candidates = [
            ("module.aquifer.aws_iam_role.server", "iam_role", f"{n}-server", f"{n}-server"),
            ("module.aquifer.aws_iam_role.dlm[0]", "iam_role", f"{n}-snapshots",
             f"{n}-snapshots"),
            ("module.aquifer.aws_iam_instance_profile.server", "iam_instance_profile",
             f"{n}-server", f"{n}-server"),
            ("module.aquifer.aws_ssm_parameter.config", "ssm_parameter", f"/aquifer/{n}/config",
             f"/aquifer/{n}/config"),
            ("module.aquifer.aws_cloudwatch_log_group.server", "log_group", f"/aquifer/{n}",
             f"/aquifer/{n}"),
        ]
        for address, kind, lookup, import_id in candidates:
            if address in state:
                continue
            tags = self.aws.tags_of(kind, lookup)
            if tags is None:
                continue
            if not self._owned(tags):
                raise InstallError(
                    f"{lookup} exists but is not tagged as part of deployment {n!r}.",
                    "It belongs to something else: choose another --name.")
            self._import(address, import_id, wheel)
            state.add(address)
        policy = "module.aquifer.aws_iam_role_policy.server"
        if policy not in state and "module.aquifer.aws_iam_role.server" in state and \
                self.aws.role_policy_exists(f"{n}-server", "aquifer"):
            self._import(policy, f"{n}-server:aquifer", wheel)

    def _import(self, address: str, import_id: str, wheel: str) -> None:
        r = self._run(self._tf("import", "-input=false", "-no-color", "-lock-timeout=60s",
                               f"-var=wheel_path=.build/dist/{wheel}", address, import_id))
        if r.returncode != 0:
            raise self._terraform_error(f"Could not adopt {import_id} into the state",
                                        r.stdout + r.stderr)
        self.c.ok(f"Adopted {import_id}: created by the interrupted run but not recorded "
                  f"({address})")

    def _report_untracked(self) -> None:
        """Resources tagged for this deployment that the state does not track: reported with
        how to remove them, never deleted automatically."""
        try:
            arns = self.aws.tagged_resources(self.settings.name)
        except Exception:  # noqa: BLE001
            self.c.warn("Could not list this deployment's tagged resources to look for "
                        "leftovers of the interrupted run.")
            return
        shown = self._run(self._tf("show", "-json"))
        known: set[str] = set()

        def collect(value) -> None:  # noqa: ANN001
            if isinstance(value, str):
                known.add(value)
            elif isinstance(value, dict):
                for v in value.values():
                    collect(v)
            elif isinstance(value, list):
                for v in value:
                    collect(v)

        try:
            collect(json.loads(shown.stdout or "{}").get("values", {}))
        except ValueError:
            return
        untracked = [a for a in arns if ":snapshot/" not in a and a not in known
                     and re.split(r"[/:]", a)[-1] not in known]
        if not untracked:
            self.c.ok("No untracked resources left by the interrupted run")
            return
        self.c.warn(f"{len(untracked)} resource(s) tagged for {self.settings.name!r} are not in "
                    "the Terraform state (created while a run was interrupted). They are not "
                    "used; remove them to avoid duplicates and cost:")
        for arn in untracked:
            self.c.info(f"  {arn}")
            rid = re.split(r"[/:]", arn)[-1]
            if ":dlm:" in arn:
                self.c.info(f"    aws dlm delete-lifecycle-policy --region {self.settings.region} "
                            f"--policy-id {rid}")
            elif ":volume/" in arn:
                self.c.info(f"    aws ec2 delete-volume --region {self.settings.region} "
                            f"--volume-id {rid}")
            elif ":vpc/" in arn:
                self.c.info(f"    aws ec2 delete-vpc --region {self.settings.region} "
                            f"--vpc-id {rid}")
            elif ":elastic-ip/" in arn:
                self.c.info(f"    aws ec2 release-address --region {self.settings.region} "
                            f"--allocation-id {rid}")
            elif ":instance/" in arn:
                self.c.info(f"    aws ec2 terminate-instances --region {self.settings.region} "
                            f"--instance-ids {rid}")

    def _state_name(self) -> str | None:
        r = self._run(self._tf("output", "-raw", "ssm_prefix"))
        if r.returncode != 0 or not r.stdout.startswith("/aquifer/"):
            return None
        return r.stdout.strip().removeprefix("/aquifer/")

    def _refuse_unmanaged_duplicate(self) -> None:
        """A fresh state must not create a second copy of a deployment someone else manages."""
        s = self.settings
        found = []
        if self.aws.role_exists(f"{s.name}-server"):
            found.append(f"IAM role {s.name}-server")
        if self.aws.parameter_exists(f"/aquifer/{s.name}/config"):
            found.append(f"SSM parameter /aquifer/{s.name}/config")
        if found:
            raise InstallError(
                f"A deployment named {s.name!r} already exists in this account but not in "
                f"this Terraform state ({', '.join(found)}).",
                "Use the state it was created with (--backend s3 --state-bucket … --state-key "
                "…, or the checkout that holds its terraform.tfstate), or choose another --name. "
                "Creating it again would fail on duplicate names.")

    def _build_wheel(self) -> str:
        dist = self.build / "dist"
        shutil.rmtree(dist, ignore_errors=True)
        dist.mkdir(parents=True)
        r = self.sys.run([sys.executable, "-m", "pip", "wheel", "-q", "--no-deps", "-w",
                          str(dist), str(self.repo_root)])
        wheels = sorted(p.name for p in dist.glob("*.whl"))
        if r.returncode != 0 or not wheels:
            raise InstallError("Building the server package failed.",
                               (r.stderr or r.stdout or "").strip()[-800:])
        self.c.ok(f"Server package {wheels[0]}")
        return wheels[0]

    def _show_plan(self) -> None:
        r = self.review
        if r.moved:
            self.c.ok(f"{r.moved} resources re-addressed into the module (state only)")
        if not r.changes:
            self.c.ok("No changes: the deployment already matches this configuration.")
            return
        counts = {"create": r.create, "update in place": r.update, "replace": r.replace,
                  "destroy": r.delete}
        for label, addrs in counts.items():
            if addrs:
                kinds: dict[str, int] = {}
                for a in addrs:
                    kinds[tf.resource_type(a)] = kinds.get(tf.resource_type(a), 0) + 1
                detail = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
                self.c.info(f"{label:<16} {len(addrs):>2}: {detail}")
        if r.data_loss:
            self.c.fail("This plan REPLACES OR DESTROYS THE DATA VOLUME (the index): "
                        + ", ".join(r.data_loss))
        if r.tainted:
            self.c.info("Re-created because an earlier run was interrupted while creating "
                        "them (Terraform marked them tainted): " + ", ".join(r.tainted))
            if any(a.endswith(tf.DATA_RESOURCES) for a in r.tainted) and not r.data_loss:
                self.c.info("No server has attached that data volume yet, so it holds no data.")
        if r.disruptive:
            self.c.warn("This plan replaces the server or its attachment (the index is kept on "
                        "the data volume; the service restarts): " + ", ".join(r.disruptive))

    def _terraform_error(self, title: str, text: str) -> InstallError:
        lock = re.search(r"Error acquiring the state lock.*?ID:\s+(\S+)", text, re.S)
        if lock:
            return InstallError(
                "The Terraform state is locked by another run.",
                "Wait for it to finish. If no other run is active (e.g. one was killed), "
                f"release the lock: terraform -chdir={self.tfdir} force-unlock {lock.group(1)}")
        errors = re.findall(r"Error: (.+)", text)
        detail = "\n".join(dict.fromkeys(errors)) or text.strip()[-800:]
        return InstallError(title, detail + "\n" + self.resume_hint())

    def approve(self) -> bool:
        r = self.review
        if r.data_loss and not self.o.allow_data_replacement:
            raise InstallError(
                "Refusing to apply a plan that replaces or destroys the data volume.",
                "Find the cause (e.g. a changed region or AZ). If losing the index is "
                "intended, re-run with --allow-data-replacement.")
        if not r.changes:
            return True
        if self.o.yes:
            self.c.ok("Approved with --yes")
            return True
        if not self.c.interactive:
            self.c.warn("Not deploying: re-run with --yes to apply after reviewing the plan "
                        "above (a fresh plan is made each run).")
            return False
        if self.c.confirm("Deploy exactly this plan?"):
            return True
        self.c.warn("Not deployed. Nothing was changed.")
        return False

    # 4. deploy ------------------------------------------------------------------------------

    def deploy(self) -> None:
        self.c.step(4, "Deploy")
        r = self.review
        plan = self.build / "plan.tfplan"
        if r.changes or r.moved:
            self._record("applying")
            started = self.sys.monotonic()

            def progress(line: str) -> None:
                m = re.search(r"([\w.\[\]-]+): (Creation complete|Modifications complete|"
                              r"Destruction complete|Still creating|Still modifying)", line)
                if m and "Still" not in m.group(2):
                    self.c.ok(f"{m.group(1)}: {m.group(2).lower()}")
                elif m:
                    self.c.info(f"{m.group(1)}: {m.group(2).lower()} "
                                f"({int(self.sys.monotonic() - started)}s)")

            try:
                rc, text = self.sys.stream(self._tf("apply", "-input=false", "-no-color",
                                                    "-lock-timeout=60s", str(plan)),
                                           str(self.tfdir), self._env(), progress)
            finally:
                plan.unlink(missing_ok=True)  # a plan is applied at most once
            if rc != 0:
                raise self._terraform_error(
                    "terraform apply stopped part-way. What was created is recorded in the "
                    "state; data was not replaced.", text)
            self.c.ok("Infrastructure applied")
        self._record("applied")
        outputs = self._outputs()
        instance = outputs["instance_id"]
        self._wait_healthy(instance)
        new_server = any(a.endswith("aws_instance.server") for a in r.create + r.replace)
        if self.existing and not new_server and any(
                a.endswith(("aws_s3_object.wheel", "aws_ssm_parameter.config"))
                for a in r.update + r.create + r.replace):
            self.c.ok("Rolling out the new server package and configuration")
            status, out = self.aws.run_remote(instance, "/opt/aquifer/bin/aquifer-update")
            if status != "Success":
                raise InstallError("The rollout on the server failed.",
                                   out.strip()[-800:] + "\n" + self.resume_hint())
        self._check_secrets(outputs)
        self._record("healthy")

    def _outputs(self) -> dict:
        r = self._run(self._tf("output", "-json"))
        if r.returncode != 0:
            raise InstallError("Could not read Terraform outputs.", r.stderr.strip())
        return {k: v["value"] for k, v in json.loads(r.stdout).items()}

    def _wait_healthy(self, instance: str, timeout_s: int = 1500) -> None:
        self.c.ok(f"Waiting for the server ({instance}) to report healthy; a first boot "
                  "installs Python and Aquifer (~3-5 minutes)")
        start = self.sys.monotonic()
        last_note = 0.0
        online = False
        while self.sys.monotonic() - start < timeout_s:
            elapsed = self.sys.monotonic() - start
            if not online:
                online = self.aws.instance_online(instance)
            if online:
                status, _ = self.aws.run_remote(
                    instance, "grep -q 'bootstrap complete' /var/log/aquifer-bootstrap.log && "
                    f"curl -fsS http://127.0.0.1:{REMOTE_PORT}/healthz", timeout_s=60)
                if status == "Success":
                    self.c.ok(f"Server healthy after {int(elapsed)}s")
                    return
            if elapsed - last_note >= 30:
                self.c.info(f"still starting ({int(elapsed)}s): "
                            f"{'installing' if online else 'waiting for the SSM agent'}")
                last_note = elapsed
            self.sys.sleep(10)
        tail = ""
        if online:
            _, tail = self.aws.run_remote(instance, "tail -30 /var/log/aquifer-bootstrap.log",
                                          timeout_s=60)
        raise InstallError("The server did not become healthy in time.",
                           (tail.strip() + "\n" if tail else "") +
                           "Logs: deploy/aws/aquifer-aws logs 30. " + self.resume_hint())

    def _check_secrets(self, outputs: dict) -> None:
        prefix = outputs["ssm_prefix"]
        if self.aws.parameter_exists(f"{prefix}/mcp-tokens"):
            self.c.ok(f"Access tokens stored at ssm:{prefix}/mcp-tokens (SecureString)")
        else:
            self.c.warn("The access token has not been created yet; the server creates it on "
                        "first start. Check: deploy/aws/aquifer-aws status")
        if outputs.get("repos") and not self.aws.parameter_exists(f"{prefix}/github-token"):
            self.c.warn("terraform.tfvars lists repositories but no GitHub token is stored. "
                        "Store one: deploy/aws/aquifer-aws github-token (copies `gh auth "
                        "token` through a 0600 file), or connect GitHub in the UI.")

    # 5. open ---------------------------------------------------------------------------------

    def open_product(self) -> None:
        self.c.step(5, "Open Aquifer")
        outputs = self._outputs()
        s = self.settings
        port = self.o.local_port
        if s.access == "https" and self._https_ready(outputs):
            ui = f"https://{s.domain}/"
            mcp = f"https://{s.domain}/mcp"
        else:
            if s.access == "https":
                self.c.warn(f"https://{s.domain}/ is not reachable yet. Create a DNS A record "
                            f"{s.domain} → {outputs['public_ip']}; the certificate follows "
                            "within minutes. Meanwhile, opening through the SSM tunnel.")
            self._tunnel(outputs["instance_id"], port)
            ui = f"http://127.0.0.1:{port}/"
            mcp = f"http://127.0.0.1:{port}/mcp"
        ops = "deploy/aws/aquifer-aws"
        self.c.ok(f"Aquifer UI: {ui}")
        self.c.info()
        self.c.info("Sign in with an access token. The admin token (`default`) is stored at")
        self.c.info(f"  ssm:{outputs['ssm_prefix']}/mcp-tokens  (SecureString; never shown here)")
        self.c.info("Copy it to the clipboard without displaying it:")
        self.c.info(f"  {ops} token | pbcopy        (Linux: | xclip -selection clipboard)")
        self.c.info("Who can do what: every valid token can read everything this deployment "
                    "indexes (one trust domain);")
        self.c.info("only admin principals (default: the `default` token) add sources and "
                    "manage tokens. Create one token per agent in the UI (Agents).")
        self.c.info()
        self.c.info("Next: Sources → Add a source → GitHub. Choose repositories, preview what "
                    "will be read, then enable indexing.")
        self.c.info("Sources added in the UI are stored on the server (data volume and SSM), "
                    "independent of Terraform.")
        self.c.info()
        self.c.info(f"MCP endpoint: {mcp}")
        self.c.info(f"  claude mcp add --transport http aquifer {mcp} --header "
                    f"\"Authorization: Bearer $({ops} token)\"")
        if s.access == "private" or not ui.startswith("https"):
            self.c.info()
            self.c.info(f"The SSM tunnel is what gives you access: it forwards localhost:{port} "
                        "to the server, which has no open ports.")
            self.c.info(f"It runs in the background until you stop it or your machine sleeps "
                        f"or restarts. Reopen it any time: {ops} connect")
            self.c.info(f"Stop it: kill $(cat {self.build}/forward.pid)")
        self.c.kv("UI_URL", ui)
        self.c.kv("MCP_URL", mcp)
        self.c.kv("MCP_TOKENS_REF", f"ssm:{outputs['ssm_prefix']}/mcp-tokens")
        if not self.o.no_browser:
            self.sys.open_browser(ui)

    def _https_ready(self, outputs: dict) -> bool:
        ip = self.sys.resolve(self.settings.domain)
        if ip != outputs["public_ip"]:
            return False
        return self.sys.http_ok(f"https://{self.settings.domain}/healthz")

    def _tunnel(self, instance: str, port: int) -> None:
        url = f"http://127.0.0.1:{port}/healthz"
        if self.sys.port_open(port):
            if self.sys.http_ok(url):
                self.c.ok(f"SSM tunnel already running on localhost:{port}")
                return
            raise InstallError(f"Local port {port} is in use by another program.",
                               "Free it, or pass --local-port <port>.")
        self.build.mkdir(parents=True, exist_ok=True)
        log = open(self.build / "forward.log", "w")  # noqa: SIM115 - owned by the child
        proc = self.sys.popen(
            ["aws", "ssm", "start-session", "--region", self.settings.region,
             "--target", instance, "--document-name", "AWS-StartPortForwardingSession",
             "--parameters", json.dumps({"portNumber": [str(REMOTE_PORT)],
                                         "localPortNumber": [str(port)]})],
            stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True, env=self._env())
        (self.build / "forward.pid").write_text(str(proc.pid))
        for _ in range(45):
            if self.sys.port_open(port) and self.sys.http_ok(url):
                self.c.ok(f"SSM tunnel open: localhost:{port} → server port {REMOTE_PORT} "
                          f"(pid {proc.pid})")
                self.c.kv("TUNNEL_PID", str(proc.pid))
                return
            self.sys.sleep(1)
        detail = (self.build / "forward.log").read_text()[-600:] \
            if (self.build / "forward.log").is_file() else ""
        raise InstallError("The SSM tunnel did not start.",
                           detail + "\nCheck session-manager-plugin and ssm:StartSession "
                           "permission, then: deploy/aws/aquifer-aws connect")


# --- CLI -------------------------------------------------------------------------------------

HELP = """Install Aquifer on AWS with Terraform (small profile: one server, ~$19/month).

Steps: 1 check prerequisites, 2 configure, 3 prepare state and review the plan, 4 deploy,
5 open the UI (through an SSM tunnel for private access). Nothing is created before you
approve the plan. Re-run the same command to resume or upgrade; it is safe to repeat.

Examples:
  aquifer install aws                                  # guided, interactive
  aquifer install aws --check-only                     # prerequisites only
  aquifer install aws --region eu-west-1 --size small --yes
  aquifer install aws --backend s3 --state-bucket my-tf-state --state-lock lockfile
  aquifer install aws --access https --domain aquifer.example.com

Docs: docs/install-aws.md
"""


def add_parser(install_sub) -> None:  # noqa: ANN001
    p = install_sub.add_parser("aws", help="install on AWS with Terraform (guided)",
                               description=HELP,
                               formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_argument_group("deployment")
    g.add_argument("--region", help="AWS region (default eu-west-1, or the existing one)")
    g.add_argument("--name", help="deployment name (default aquifer)")
    g.add_argument("--size", choices=list(tf.SIZES), help="server size (default small)")
    g.add_argument("--access", choices=["private", "https"],
                   help="private: SSM tunnel, no inbound ports (default); https: public domain")
    g.add_argument("--domain", help="DNS name for --access https")
    g.add_argument("--https-cidr", action="append", dest="https_cidrs", default=[],
                   metavar="CIDR", help="source range allowed to reach HTTPS (repeatable)")
    g.add_argument("--extraction-model", metavar="auto|none|MODEL_ID",
                   help="Claude model for LLM extraction and grounded answers (default auto)")
    g = p.add_argument_group("Terraform state")
    g.add_argument("--backend", choices=["local", "s3"],
                   help="local file in deploy/aws (default) or your team's S3 bucket")
    g.add_argument("--state-bucket", help="existing S3 bucket for --backend s3")
    g.add_argument("--state-key",
                   help="state object key (default aquifer/<name>/terraform.tfstate)")
    g.add_argument("--state-region", help="bucket region (default: the deployment region)")
    g.add_argument("--state-lock", metavar="lockfile|dynamodb:TABLE",
                   help="locking for --backend s3 (default lockfile, Terraform >= 1.10)")
    g = p.add_argument_group("run")
    g.add_argument("--yes", action="store_true",
                   help="non-interactive: use flags and defaults, approve the reviewed plan")
    g.add_argument("--check-only", action="store_true", help="only check prerequisites")
    g.add_argument("--plan-only", action="store_true", help="stop after showing the plan")
    g.add_argument("--no-browser", action="store_true", help="print the UI URL, do not open it")
    g.add_argument("--local-port", type=int, default=18080, help="tunnel port (default 18080)")
    g.add_argument("--allow-data-replacement", action="store_true",
                   help="allow a plan that replaces the data volume (loses the index)")
    g.add_argument("--source-dir", help="Aquifer source checkout (default: this installation)")
    p.set_defaults(fn=cmd_install_aws)


def cmd_install_aws(args: argparse.Namespace) -> int:
    interactive = sys.stdin.isatty() and not args.yes
    opts = Options(
        region=args.region, name=args.name, size=args.size, access=args.access,
        domain=args.domain, https_cidrs=args.https_cidrs,
        extraction_model=args.extraction_model, backend=args.backend,
        state_bucket=args.state_bucket, state_key=args.state_key,
        state_region=args.state_region, state_lock=args.state_lock,
        local_port=args.local_port, source_dir=args.source_dir, yes=args.yes,
        check_only=args.check_only, plan_only=args.plan_only, no_browser=args.no_browser,
        allow_data_replacement=args.allow_data_replacement, interactive=interactive)
    return Installer(opts, Console(interactive=interactive)).run()
