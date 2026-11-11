# Installing Aquifer on AWS

The recommended path is the guided installer, **`aquifer install aws`**. It drives Terraform
(the reusable module in [`deploy/aws/modules/aquifer`](../deploy/aws/modules/aquifer)) and ends
with the UI open in your browser. Teams that manage infrastructure in their own Terraform can
[call the module directly](#direct-terraform) instead.

What you get: one ARM server (EC2 t4g.small by default, ~$19/month in eu-west-1, plus model
usage), an encrypted data volume with daily snapshots, no inbound ports. You reach the UI and the
MCP endpoint through an SSM tunnel from your machine, or over HTTPS on your own domain if you
choose that. A fresh installation has **no data sources**: you connect them in the UI.

## 1. Get the CLI

Aquifer is installed from its source repository (the `aquifer` package on PyPI is an unrelated
project; do not `pip install aquifer`). The installer needs the checkout: it contains the
Terraform module and builds the server package from it.

```bash
git clone https://github.com/Senora-dev/aquifer-ai.git && cd aquifer-ai
python3 -m venv .venv && .venv/bin/pip install -e ".[server]"
.venv/bin/aquifer install aws --help
```

Python 3.11 or newer. To pin a release, `git checkout <tag>` before installing.

You also need, on the same machine:

| Tool | Why |
| --- | --- |
| Terraform ≥ 1.6 (≥ 1.10 for S3 state with native locking) | applies the module |
| AWS CLI v2, logged in (`aws sso login` or a profile) | credentials; the SSM tunnel |
| AWS Session Manager plugin | the SSM tunnel (private access) |

Docker is not needed. The installer checks all of this first and tells you what to fix.

## 2. Run the installer

```bash
.venv/bin/aquifer install aws
```

It walks through five steps. Nothing is created before you approve the plan in step 3.

1. **Check prerequisites**: tools and versions, AWS login (shows the account and identity),
   region enabled, instance type offered, VPC and Elastic IP quotas, IAM permissions (by policy
   simulation; SCPs are not evaluated), access to registry.terraform.io and pypi.org, and Bedrock:
   Titan Text Embeddings v2 is required; a Claude model enables LLM extraction and answers with
   citations (auto-detected, or `--extraction-model`). Every blocker comes with a fix; on any
   blocker the installer stops with nothing created.
2. **Configure**: name, size, access mode, model, state location. It prints them with the AWS
   account and region, and writes `deploy/aws/terraform.tfvars`.
3. **Prepare state and review**: initializes the backend (see [state](#where-the-terraform-state-lives)),
   builds the server package, runs `terraform plan`, and shows what will be created, changed or
   replaced, with a cost estimate (list prices of eu-west-1; other regions differ). It refuses
   any plan that would replace or destroy the data volume unless you pass
   `--allow-data-replacement`.
4. **Deploy**: applies exactly the reviewed plan, shows progress, waits until the server is
   healthy (first boot ~3–5 minutes). On an existing deployment it rolls out the new package.
5. **Open Aquifer**: opens the SSM tunnel (private access), waits until the UI answers through
   it, prints the UI and MCP addresses, and opens the browser.

### Options for automation

| Flag | Meaning |
| --- | --- |
| `--region REGION` | default `eu-west-1`, or the existing deployment's |
| `--name NAME` | default `aquifer`; prefixes every resource and `/aquifer/<name>/` in SSM |
| `--size small\|medium\|large` | t4g.small / medium / large; `large` exceeds the $50/month budget and needs confirmation |
| `--access private\|https`, `--domain`, `--https-cidr CIDR` | see [access](#opening-the-ui-and-the-tunnel) |
| `--extraction-model auto\|none\|MODEL_ID` | Claude model for extraction and answers |
| `--backend local\|s3`, `--state-bucket`, `--state-key`, `--state-region`, `--state-lock lockfile\|dynamodb:TABLE` | [state](#where-the-terraform-state-lives) |
| `--yes` | non-interactive: flags and defaults, apply the reviewed plan |
| `--check-only` / `--plan-only` | stop after step 1 / step 3 |
| `--no-browser`, `--local-port PORT` | print the URL only; tunnel port (default 18080) |
| `--allow-data-replacement` | allow a plan that replaces the data volume (loses the index) |
| `--source-dir PATH` | the Aquifer checkout, if not the one the CLI was installed from |

Without `--yes`, a non-interactive run stops after showing the plan. Lines like `UI_URL=…`,
`MCP_URL=…` and `MCP_TOKENS_REF=…` on stdout are for scripts and agents.

```bash
aquifer install aws --region eu-west-1 --size small --yes --no-browser
aquifer install aws --backend s3 --state-bucket my-tf-state --state-lock lockfile
```

## Where the Terraform state lives

The installer always says where it is. Two options:

- **Local** (default): `deploy/aws/terraform.tfstate` in your checkout. Only that machine can
  operate the deployment; back the file up.
- **S3** (teams): `--backend s3 --state-bucket <existing bucket>` writes `deploy/aws/backend.tf`
  (key `aquifer/<name>/terraform.tfstate` by default, encrypted) and locks with an S3 lock file
  (`--state-lock lockfile`, Terraform ≥ 1.10) or your DynamoDB table
  (`--state-lock dynamodb:<table>`). The bucket must exist; versioning is recommended. Switching
  an existing local deployment copies its state after confirmation (`-migrate-state`).

A `backend.tf` you already have is used as is. One checkout (`deploy/aws`) manages one
deployment; for a second one use another clone, or the module from your own Terraform root.

The state contains no secrets: the GitHub token, source credentials and access tokens are in SSM
Parameter Store (SecureString) under `/aquifer/<name>/`.

## Opening the UI and the tunnel

**Private access (default).** The server has no inbound ports. The installer starts an SSM port
forward, `localhost:18080 → server:8080`; that tunnel **is** your access:

- UI: <http://127.0.0.1:18080/> · MCP: `http://127.0.0.1:18080/mcp`
- It runs in the background until you stop it, or your machine sleeps or restarts.
- Reopen it any time: `deploy/aws/aquifer-aws connect` (or re-run `aquifer install aws`).
- Stop it: `kill $(cat deploy/aws/.build/forward.pid)`.

**HTTPS access.** `aquifer install aws --access https --domain aquifer.example.com
[--https-cidr <range>]`, then point a DNS A record at the `public_ip` output. Caddy on the server
gets a Let's Encrypt certificate. Until DNS resolves, the installer opens the UI through the
tunnel. Every request still needs a token.

**Sign in** with the admin token (`default`), created by the server on first start and stored at
`ssm:/aquifer/<name>/mcp-tokens`. Copy it without displaying it:

```bash
deploy/aws/aquifer-aws token | pbcopy        # Linux: | xclip -selection clipboard
```

Who can do what: every valid token can read everything the deployment indexes (one trust
domain); only admin principals (`default` unless `AQUIFER_WEB__ADMIN_PRINCIPALS` says otherwise)
add sources and manage tokens. Agents get their own read-only credentials:
[docs/agent-integration.md](agent-integration.md).

## Connecting the first source

In the UI: **Sources → Add a source → GitHub**.

1. **Token**: a fine-grained token with read-only Contents, Issues, Pull requests and Metadata.
   Aquifer checks it with GitHub and stores it in SSM (`/aquifer/<name>/sources/native-github/token`).
2. **Repositories**: choose from the ones the token can read.
3. **Preview**: what indexing would read from each repository (README, matching files such as
   ADRs, Terraform and Compose, issues and pull requests), from read-only calls; nothing is
   stored yet.
4. **Review access**, then **Enable indexing**. Progress shows per repository.

These settings live on the server (`/var/lib/aquifer/github-source.json` on the data volume, the
token in SSM), not in Terraform: adding or removing sources needs no redeploy. Repositories can
also be set in `terraform.tfvars` (`repos = [...]`, with `deploy/aws/aquifer-aws github-token`);
both lists are combined. Other tools (Discussions, Jira, Linear, …): [integrator.md](integrator.md).

## Upgrading

```bash
git pull                                  # or: git checkout <newer tag>
.venv/bin/pip install -e ".[server]"
.venv/bin/aquifer install aws             # shows the plan; usually 1–2 in-place updates
```

The installer keeps your `terraform.tfvars` (repositories, size, extra settings), never shrinks
the data volume, and rolls the new package out on the server. Deployments created before the
module existed are upgraded in place: `moved` blocks re-address their 23 resources in state, so
nothing is destroyed or recreated (verified on a live deployment: 23 moved, 0 changes).
`deploy/aws/aquifer-aws deploy` does the same without the review step.

## When something fails

Re-run the same command. Terraform state records what exists, so the next plan contains only
what is left and nothing is created twice.

| Symptom | What it means / what to do |
| --- | --- |
| `AWS credentials are missing or expired` / `AWS profile '…' does not exist` | `aws sso login`, or set `AWS_PROFILE` to an existing profile |
| `VPCs: 5 of 5 used` (or Elastic IPs) | Free one, request a quota increase, or use another `--region` |
| `Bedrock embeddings … cannot be invoked` | Enable model access for Titan Text Embeddings v2 in the Bedrock console of that region |
| `terraform apply stopped part-way` | Fix the cause shown (capacity, quota, permission), then re-run |
| Interrupted run (Ctrl-C) | Re-run. Resources whose creation was interrupted are re-created (Terraform marks them tainted); the installer allows replacing the data volume in that case only when no server has used it yet. Resources AWS finished creating after the interrupt are adopted into the state when their tags name this deployment, and other leftovers are listed with the command to delete them |
| `The Terraform state is locked by another run` | Wait; if no run is active, the message gives the `terraform force-unlock` command |
| `A deployment named … already exists in this account but not in this Terraform state` | Use the state it was created with (its checkout or `--backend s3 …`), or another `--name` |
| `Refusing to apply a plan that replaces … the data volume` | Something changed the volume's identity (region, AZ). Investigate before `--allow-data-replacement` |
| Server not healthy in time | `deploy/aws/aquifer-aws logs 30`; re-run to keep waiting |
| Tunnel did not start | Session Manager plugin installed? `ssm:StartSession` allowed? Then `deploy/aws/aquifer-aws connect` |

`.build/install-state.json` records the last phase reached; operations commands
(`deploy/aws/aquifer-aws status`, `logs`, `sync`, …) work on an installer-made deployment:
[deploy/aws/README.md](../deploy/aws/README.md).

## Removing it

```bash
deploy/aws/aquifer-aws down
```

Destroys every resource of the deployment (asks first) and deletes `/aquifer/<name>/` from SSM.
Existing EBS snapshots are kept; delete them in the console if you do not need them. With S3
state, the state object stays in your bucket (now empty of resources).

## Direct Terraform

The module is [`deploy/aws/modules/aquifer`](../deploy/aws/modules/aquifer/README.md); a minimal
root module is [`deploy/aws/examples/minimal`](../deploy/aws/examples/minimal/main.tf).

```bash
cd deploy/aws/examples/minimal
pip wheel --no-deps -w dist ../../../..      # the server package the instance installs
terraform init
terraform plan -out tfplan                   # add -var name=… -var region=…
terraform apply tfplan
```

From another repository, pin the module and the wheel to the same release tag:

```hcl
module "aquifer" {
  source     = "git::https://github.com/Senora-dev/aquifer-ai.git//deploy/aws/modules/aquifer?ref=v0.2.0"
  name       = "aquifer"
  wheel_path = "${path.module}/dist/aquifer-0.2.0-py3-none-any.whl"
}
```

```bash
pip wheel --no-deps -w dist "git+https://github.com/Senora-dev/aquifer-ai.git@v0.2.0"
```

Configure the `aws` provider (region) and your backend in the root module. After apply, open the
UI with the tunnel from the `open_the_ui` output (or `aws ssm start-session …
AWS-StartPortForwardingSession`), then <http://127.0.0.1:18080/>. The admin token is the
`default` entry of the JSON in `mcp_tokens_ref`:

```bash
aws ssm get-parameter --name /aquifer/<name>/mcp-tokens --with-decryption \
  --query Parameter.Value --output text | python3 -c 'import json,sys; print(json.load(sys.stdin)["default"], end="")' | pbcopy
```

## Verification

How this path was verified (November 2026):

| Check | How |
| --- | --- |
| Fresh install, existing deployment, missing tools/credentials/Bedrock access, failed and interrupted runs, quota blockers, leftovers, S3 backend and migration, HTTPS fallback | Offline tests with fake Terraform/AWS (`tests/test_install_aws.py`) |
| Upgrade of an existing deployment (pre-module state) | Live: `aquifer install aws` on a running deployment: 23 resources moved, 1 in-place update, rollout, healthy, tunnel reused |
| Missing credentials | Live: nonexistent profile and invalid keys → blocker, nothing created |
| Interrupted install and resume | Live (eu-west-1): Ctrl-C during apply, then re-run. Found and fixed: tainted resources, an instance profile and a DLM policy created after the interrupt but not in state, the account's VPC quota. The re-run then stopped on the quota check with nothing created; the partial deployment was removed with `aquifer-aws down` |
| Module and example | `terraform validate` and `terraform fmt -check` |
