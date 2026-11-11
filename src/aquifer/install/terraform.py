"""Terraform files and plans for ``aquifer install aws``: sizes, tfvars, backend, plan review.

The installer drives the same root module as ``deploy/aws/aquifer-aws`` (``deploy/aws``), with
the same ``terraform.tfvars``, ``.build/`` directory and state, so both keep working on one
deployment.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# name: (instance type, data volume GiB, baseline USD/month in eu-west-1, capacity).
# Keep in sync with size_spec() in deploy/aws/aquifer-aws (tests/test_install_aws.py checks).
SIZES: dict[str, tuple[str, int, float, str]] = {
    "small": ("t4g.small", 10, 19.16, "up to ~50 repositories / ~5,000 documents (measured)"),
    "medium": ("t4g.medium", 20, 33.47, "up to ~200 repositories / ~20,000 documents (estimated)"),
    "large": ("t4g.large", 40, 62.10, "more than that (estimated); above the $50/month budget"),
}
PRICED_REGION = "eu-west-1"
COST_PER_DOCUMENT_USD = 0.0044  # Claude Haiku 4.5 extraction, measured (docs/cost.md)

# Resources whose replacement or deletion loses data or access. Replacing the instance keeps
# the index (it lives on the separate data volume) but restarts the service.
DATA_RESOURCES = ("aws_ebs_volume.data",)
DISRUPTIVE_RESOURCES = ("aws_instance.server", "aws_volume_attachment.data", "aws_eip.server")


def size_for_instance(instance_type: str) -> str | None:
    return next((k for k, v in SIZES.items() if v[0] == instance_type), None)


def cost_lines(size: str, region: str, extraction_model: str, sources: bool = False) -> list[str]:
    """A qualified estimate: list prices of one region, before usage."""
    instance, gb, monthly, _ = SIZES[size]
    lines = [f"Baseline ~${monthly:.2f}/month: {instance}, 8 GiB root + {gb} GiB data volume, "
             "Elastic IP, snapshots and logs (on-demand list prices, 730 h, "
             f"{PRICED_REGION})."]
    if region != PRICED_REGION:
        lines.append(f"{region} prices differ from {PRICED_REGION}; check the AWS Pricing "
                     "Calculator for the exact figure.")
    if extraction_model:
        lines.append(f"Usage on top: ~${COST_PER_DOCUMENT_USD} per new or changed document "
                     "for extraction (measured with Claude Haiku 4.5), plus embeddings and "
                     "grounded answers; unchanged documents cost nothing (docs/cost.md).")
    else:
        lines.append("Usage on top: embeddings only (LLM extraction is off); see docs/cost.md.")
    if not sources:
        lines.append("No data source is connected yet, so there is no model usage until you "
                     "add one in the UI.")
    lines.append("Estimates, not a quote.")
    return lines


# --- terraform.tfvars ------------------------------------------------------------------------

_LINE = re.compile(r"^(?P<key>[a-z_]+)\s*=\s*(?P<value>.+?)\s*$")


def read_tfvars(path: Path) -> dict:
    """The simple ``key = value`` assignments this installer and ``aquifer-aws`` write."""
    out: dict = {}
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        m = _LINE.match(line.strip())
        if not m or line.lstrip().startswith("#"):
            continue
        raw = m.group("value").split(" #", 1)[0].strip()
        try:
            out[m.group("key")] = json.loads(raw)
        except json.JSONDecodeError:
            out[m.group("key")] = raw
    return out


def _hcl(value) -> str:  # noqa: ANN001
    return json.dumps(value)


def update_tfvars(path: Path, values: dict, header: str) -> None:
    """Set ``values`` in place, keeping every other line (comments, repos, extra settings)."""
    lines = path.read_text().splitlines() if path.is_file() else [header]
    seen = set()
    for i, line in enumerate(lines):
        m = _LINE.match(line.strip())
        if m and not line.lstrip().startswith("#") and m.group("key") in values:
            key = m.group("key")
            lines[i] = f"{key:<19} = {_hcl(values[key])}"
            seen.add(key)
    for key, value in values.items():
        if key not in seen:
            lines.append(f"{key:<19} = {_hcl(value)}")
    path.write_text("\n".join(lines) + "\n")


# --- backend ---------------------------------------------------------------------------------

BACKEND_FILE = "backend.tf"


@dataclass
class Backend:
    kind: str = "local"  # local | s3
    bucket: str = ""
    key: str = ""
    region: str = ""
    lock: str = "lockfile"  # lockfile | dynamodb:<table>
    path: str = ""  # local state file (display only)

    def describe(self) -> str:
        if self.kind == "local":
            return (f"local file {self.path} on this machine (not shared; back it up, or use "
                    "--backend s3 for a team)")
        if self.kind != "s3":
            return f"{self.kind} backend configured in {self.path}"
        lock = ("S3 lock file (use_lockfile)" if self.lock == "lockfile"
                else f"DynamoDB table {self.lock.split(':', 1)[1]}")
        return f"s3://{self.bucket}/{self.key} ({self.region}), locking: {lock}"

    def render(self) -> str:
        settings = [("bucket", f'"{self.bucket}"'), ("key", f'"{self.key}"'),
                    ("region", f'"{self.region}"'), ("encrypt", "true")]
        if self.lock == "lockfile":
            settings.append(("use_lockfile", "true"))
        else:
            settings.append(("dynamodb_table", f'"{self.lock.split(":", 1)[1]}"'))
        width = max(len(k) for k, _ in settings)
        body = "\n".join(f"    {k:<{width}} = {v}" for k, v in settings)
        return ("# Written by `aquifer install aws`: where this deployment's Terraform state "
                "lives.\n# Everyone who operates the deployment needs the same backend: keep "
                "this file with the checkout.\n"
                f'terraform {{\n  backend "s3" {{\n{body}\n  }}\n}}\n')


_BACKEND_KV = re.compile(r'^\s*(bucket|key|region|dynamodb_table|use_lockfile)\s*=\s*"?([^"\s]+)"?',
                         re.M)


def existing_backend(tfdir: Path) -> Backend | None:
    """The backend configured in the root module's *.tf files, if any (hand-written or ours)."""
    for f in sorted(tfdir.glob("*.tf")):
        text = f.read_text()
        m = re.search(r'backend\s+"(\w+)"\s*\{', text)
        if not m:
            continue
        if m.group(1) != "s3":
            return Backend(kind=m.group(1), path=str(f))
        kv = dict(_BACKEND_KV.findall(text))
        lock = f"dynamodb:{kv['dynamodb_table']}" if kv.get("dynamodb_table") else "lockfile"
        return Backend("s3", kv.get("bucket", ""), kv.get("key", ""), kv.get("region", ""), lock)
    return None


# --- plan review -----------------------------------------------------------------------------

@dataclass
class PlanReview:
    create: list[str] = field(default_factory=list)
    update: list[str] = field(default_factory=list)
    replace: list[str] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)
    tainted: list[str] = field(default_factory=list)  # replaced because creation was interrupted
    moved: int = 0

    @property
    def changes(self) -> int:
        return len(self.create) + len(self.update) + len(self.replace) + len(self.delete)

    def touches(self, suffixes: tuple[str, ...]) -> list[str]:
        return [a for a in self.replace + self.delete if a.endswith(suffixes)]

    @property
    def new_server(self) -> bool:
        """The plan creates the server: no server has used the data volume in this state."""
        return any(a.endswith("aws_instance.server") for a in self.create) and not any(
            a.endswith("aws_volume_attachment.data") for a in self.update + self.replace)

    @property
    def data_loss(self) -> list[str]:
        """Data volume replacements or deletions that can lose the index.

        A volume whose creation was interrupted (tainted) and that no server has attached yet
        holds no data: replacing it is how an interrupted first installation resumes.
        """
        return [a for a in self.touches(DATA_RESOURCES)
                if not (a in self.tainted and a in self.replace and self.new_server)]

    @property
    def disruptive(self) -> list[str]:
        return self.touches(DISRUPTIVE_RESOURCES)


def review_plan(plan_json: dict) -> PlanReview:
    r = PlanReview()
    for rc in plan_json.get("resource_changes", []):
        actions = rc["change"]["actions"]
        addr = rc["address"]
        if rc.get("previous_address"):
            r.moved += 1
        if actions == ["create"]:
            r.create.append(addr)
        elif actions == ["update"]:
            r.update.append(addr)
        elif actions == ["delete"]:
            r.delete.append(addr)
        elif "delete" in actions and "create" in actions:
            r.replace.append(addr)
            if rc.get("action_reason") == "replace_because_tainted":
                r.tainted.append(addr)
    return r


def resource_type(address: str) -> str:
    return address.split(".")[-2] if "." in address else address
