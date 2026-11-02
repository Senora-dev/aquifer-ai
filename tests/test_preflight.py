"""Preflight script: checks are read-only and report clearly (AWS/GitHub clients are faked)."""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("preflight", ROOT / "scripts/preflight.py")
preflight = importlib.util.module_from_spec(spec)
sys.modules["preflight"] = preflight  # dataclasses resolve their module by name
spec.loader.exec_module(preflight)


class FakeClient:
    def __init__(self, fail=None, lifecycle="ACTIVE"):
        self.fail = fail or set()
        self.lifecycle = lifecycle

    def __getattr__(self, name):
        def call(**kw):
            if name in self.fail:
                raise RuntimeError(f"{name} denied")
            return {
                "get_caller_identity": {"Arn": "arn:aws:iam::000000000000:user/dev"},
                "list_collections": {},
                "describe_vpc_endpoint_services": {"ServiceNames": ["x"]},
                "get_foundation_model": {"modelDetails": {"modelLifecycle": {
                    "status": self.lifecycle}}},
                "get_inference_profile": {},
            }[name]
        return call


class FakeSession:
    def __init__(self, **kw):
        self.kw = kw

    def client(self, name):
        return FakeClient(**self.kw)


def test_aws_checks_pass_and_redact_account_id():
    checks = preflight.check_aws("eu-west-1", "amazon.titan-embed-text-v2:0",
                                 "us.anthropic.claude-x", False, session=FakeSession())
    assert all(c.status == "PASS" for c in checks)
    assert "000000000000" not in " ".join(c.detail for c in checks)


def test_unavailable_model_fails_and_legacy_model_warns():
    fail = preflight.check_aws("eu-west-1", "m1", "m2", False,
                               session=FakeSession(fail={"get_foundation_model"}))
    assert [c.status for c in fail if "model" in c.name] == ["FAIL", "FAIL"]
    legacy = preflight.check_aws("eu-west-1", "m1", "m2", False,
                                 session=FakeSession(lifecycle="LEGACY"))
    assert {c.status for c in legacy if "model" in c.name} == {"WARN"}


def test_config_errors_are_reported_not_raised():
    checks, cfg = preflight.check_config({"repos": "acme/*"})
    assert cfg is None and checks[0].status == "FAIL" and "wildcards" in checks[0].detail


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes

    def get(self, path, params=None):
        status, payload = self.routes.get(path, (404, {}))

        class R:
            status_code = status

            def json(self):
                return payload
        return R()


def test_github_checks_detect_rename_and_missing_access():
    http = FakeHttp({
        "/rate_limit": (200, {}),
        "/repos/acme/a": (200, {"full_name": "acme/a"}),
        "/repos/acme/a/issues": (200, []),
        "/repos/acme/old": (200, {"full_name": "acme/new"}),
        "/repos/acme/old/issues": (200, []),
    })
    checks = {c.name: c for c in preflight.check_github(["acme/a", "acme/old", "acme/x"], "t",
                                                        http=http)}
    assert checks["github acme/a"].status == "PASS"
    assert checks["github acme/old"].status == "WARN" and "renamed" in checks[
        "github acme/old"].detail
    assert checks["github acme/x"].status == "FAIL"
