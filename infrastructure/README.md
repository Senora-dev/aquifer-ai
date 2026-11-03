# Aquifer infrastructure (AWS CDK, Python)

The **single deployable stack**. `cdk deploy` brings up the whole system: VPC + endpoints,
OpenSearch Serverless collection, ingestion (Lambdas / SQS / EventBridge / S3 / Secrets), and
the Fargate MCP service behind an internal NLB.

```
infrastructure/
  app.py                  # CDK app entrypoint (prints configuration errors and exits)
  stack_config.py         # deploy-time configuration, validated before synthesis
  aquifer_stack.py        # the one stack, composing the components below
  components/             # (named 'components' to avoid shadowing the 'constructs' library)
    network.py            # VPC, endpoints, per-role security groups (HTTPS-only egress)
    vector_store.py       # AOSS collection + VPC endpoint + least-privilege policies
    mcp_service.py        # Fargate service, bearer-token secret, internal NLB (optional TLS)
    ingestion.py          # Discovery + Worker Lambdas, EventBridge, SQS(+DLQ, alarm), S3, Secrets
```

```bash
pip install -e ".[cdk]"                    # from the repo root
python scripts/preflight.py --region <region> --repos your-org/your-repo
cd infrastructure
cdk synth -c repos='["your-org/your-repo"]'
cdk deploy -c repos='["your-org/your-repo"]'
```

Deploying builds the Lambda bundle and the MCP image, so **Docker must be running**. All context
keys, outputs, token setup and uninstall steps: [../docs/deployment.md](../docs/deployment.md).
Costs: [../docs/operations.md](../docs/operations.md#cost). `tests/test_infrastructure.py`
synthesizes the stack offline and asserts its security properties.
