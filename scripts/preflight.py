#!/usr/bin/env python3
"""Pre-deployment checks for Aquifer. Read-only: creates nothing and incurs no AWS charges
unless you pass --invoke-test (one tiny Bedrock embedding call, a fraction of a cent).

    python scripts/preflight.py --region eu-west-1 --repos acme/payments,acme/platform
    GITHUB_TOKEN=... python scripts/preflight.py --region eu-west-1 --repos acme/payments

Checks: local toolchain (Python, Node.js, CDK CLI, Docker), deploy configuration, AWS
credentials, OpenSearch Serverless and Bedrock availability in the region (including the
configured models and the Bedrock VPC endpoint service), and — if GITHUB_TOKEN is set — that
the token can read every configured repository.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infrastructure"))

from stack_config import (  # noqa: E402
    DEFAULT_EMBEDDING_MODEL_ID,
    DEFAULT_EXTRACTION_MODEL_ID,
    ConfigError,
    load_config,
)

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    return out.returncode, (out.stdout or out.stderr).strip()


def check_toolchain() -> list[Check]:
    checks = []
    v = sys.version_info
    checks.append(Check("python >= 3.11", PASS if v >= (3, 11) else FAIL,
                        f"{v.major}.{v.minor}.{v.micro}"))
    code, out = _run(["node", "--version"])
    if code:
        checks.append(Check("node.js", FAIL, "not found (required by the CDK CLI)"))
    else:
        major = int(out.lstrip("v").split(".")[0])
        status = PASS if major % 2 == 0 and major >= 20 else WARN
        checks.append(Check("node.js (even-numbered LTS)", status, out))
    checks.append(Check("cdk CLI", PASS if shutil.which("cdk") else WARN,
                        "found" if shutil.which("cdk") else "not found; use `npx cdk`"))
    code, _ = _run(["docker", "info"])
    checks.append(Check("docker daemon", PASS if code == 0 else FAIL,
                        "running" if code == 0 else "not running (needed for asset bundling)"))
    return checks


def check_config(context: dict) -> tuple[list[Check], object | None]:
    try:
        cfg = load_config(context.get)
    except ConfigError as exc:
        return [Check("deploy configuration", FAIL, str(exc))], None
    return [Check("deploy configuration", PASS,
                  f"{len(cfg.repos)} repo(s); standby replicas {cfg.aoss_standby_replicas}")], cfg


def _model_check(bedrock, model_id: str, label: str) -> Check:
    try:
        if model_id.split(".", 1)[0] in {"us", "eu", "apac", "us-gov", "jp", "au", "ca",
                                          "global"}:
            bedrock.get_inference_profile(inferenceProfileIdentifier=model_id)
        else:
            info = bedrock.get_foundation_model(modelIdentifier=model_id)["modelDetails"]
            status = (info.get("modelLifecycle") or {}).get("status", "ACTIVE")
            if status != "ACTIVE":
                return Check(label, WARN, f"{model_id} lifecycle status is {status}")
    except Exception as exc:  # noqa: BLE001
        return Check(label, FAIL, f"{model_id}: {type(exc).__name__}: {exc}")
    return Check(label, PASS, f"{model_id} available (access must also be enabled in the "
                              "Bedrock console)")


def check_aws(region: str, embed_model: str, extract_model: str, invoke_test: bool,
              session=None) -> list[Check]:  # noqa: ANN001
    try:
        import boto3
    except ImportError:
        return [Check("boto3", FAIL, "pip install -e '.[aws]'")]
    session = session or boto3.Session(region_name=region)
    checks = []
    try:
        ident = session.client("sts").get_caller_identity()
        # The ARN identifies the caller; the account id is redacted from the output.
        checks.append(Check("aws credentials", PASS, ident["Arn"].split(":")[-1]))
    except Exception as exc:  # noqa: BLE001
        return [Check("aws credentials", FAIL, f"{type(exc).__name__}: {exc}")]

    try:
        session.client("opensearchserverless").list_collections(maxResults=1)
        checks.append(Check("opensearch serverless", PASS, f"available in {region}"))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("opensearch serverless", FAIL, f"{type(exc).__name__}: {exc}"))

    try:
        name = f"com.amazonaws.{region}.bedrock-runtime"
        found = session.client("ec2").describe_vpc_endpoint_services(ServiceNames=[name])
        ok = bool(found.get("ServiceNames"))
        checks.append(Check("bedrock VPC endpoint", PASS if ok else FAIL, name))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("bedrock VPC endpoint", FAIL, f"{type(exc).__name__}: {exc}"))

    bedrock = session.client("bedrock")
    checks.append(_model_check(bedrock, embed_model, "embedding model"))
    checks.append(_model_check(bedrock, extract_model, "extraction model"))

    if invoke_test:
        try:
            body = json.dumps({"inputText": "aquifer preflight", "dimensions": 256})
            session.client("bedrock-runtime").invoke_model(modelId=embed_model, body=body)
            checks.append(Check("bedrock invoke (embedding)", PASS, "model access enabled"))
        except Exception as exc:  # noqa: BLE001
            checks.append(Check("bedrock invoke (embedding)", FAIL,
                                f"{type(exc).__name__}: {exc}"))
    return checks


def check_github(repos: list[str], token: str, http=None) -> list[Check]:  # noqa: ANN001
    import httpx

    http = http or httpx.Client(base_url="https://api.github.com", timeout=20,
                                headers={"Authorization": f"Bearer {token}",
                                         "Accept": "application/vnd.github+json"},
                                follow_redirects=True)
    r = http.get("/rate_limit")
    if r.status_code == 401:
        return [Check("github token", FAIL, "rejected (401)")]
    checks = [Check("github token", PASS, "accepted")]
    for repo in repos:
        r = http.get(f"/repos/{repo}")
        if r.status_code != 200:
            checks.append(Check(f"github {repo}", FAIL, f"HTTP {r.status_code} (no access?)"))
            continue
        full = r.json().get("full_name", repo)
        note = f"renamed to {full}; update -c repos" if full.lower() != repo.lower() else "ok"
        issues = http.get(f"/repos/{repo}/issues", params={"per_page": 1})
        status = PASS if issues.status_code == 200 and full.lower() == repo.lower() else WARN
        detail = note if issues.status_code == 200 else f"{note}; issues HTTP {issues.status_code}"
        checks.append(Check(f"github {repo}", status, detail))
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--region", default=os.environ.get("CDK_DEFAULT_REGION")
                        or os.environ.get("AWS_REGION"))
    parser.add_argument("--repos", default="", help="comma-separated owner/repo list")
    parser.add_argument("--model-id", default=DEFAULT_EMBEDDING_MODEL_ID)
    parser.add_argument("--extraction-model-id", default=DEFAULT_EXTRACTION_MODEL_ID)
    parser.add_argument("--skip-aws", action="store_true")
    parser.add_argument("--invoke-test", action="store_true",
                        help="make one tiny Bedrock embedding call to confirm model access")
    args = parser.parse_args(argv)

    checks = check_toolchain()
    cfg_checks, cfg = check_config({"repos": args.repos, "model_id": args.model_id,
                                    "extraction_model_id": args.extraction_model_id})
    checks += cfg_checks
    if not args.skip_aws:
        if not args.region:
            checks.append(Check("aws region", FAIL, "pass --region or set CDK_DEFAULT_REGION"))
        else:
            checks += check_aws(args.region, args.model_id, args.extraction_model_id,
                                args.invoke_test)
    token = os.environ.get("GITHUB_TOKEN")
    if token and cfg is not None:
        checks += check_github(cfg.repos, token)
    elif cfg is not None:
        checks.append(Check("github access", WARN, "set GITHUB_TOKEN to verify repo access"))

    width = max(len(c.name) for c in checks)
    for c in checks:
        print(f"[{c.status}] {c.name.ljust(width)}  {c.detail}")
    failed = [c for c in checks if c.status == FAIL]
    print(f"\n{len(failed)} failure(s)." if failed else "\nAll required checks passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
