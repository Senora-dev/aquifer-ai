# Aquifer infrastructure (AWS CDK, Python)

The **single deployable stack** for Aquifer lives here. `cdk deploy` brings up the whole
Context Lake: VPC + endpoints, OpenSearch Serverless collection, ingestion (Lambdas / SQS /
EventBridge / S3 / Secrets), and the Fargate MCP service.

Planned layout (built in the CDK task):

```
infrastructure/
  app.py                  # CDK app entrypoint
  cdk.json
  aquifer_stack.py        # the ONE stack, composes the components below
  components/             # (named 'components' to avoid shadowing the 'constructs' library)
    network.py            # VPC + Bedrock/Secrets/SQS interface endpoints + S3 gateway endpoint
    vector_store.py       # AOSS collection + VPC endpoint + encryption/network/data-access policies
    mcp_service.py        # Fargate service + internal NLB
    ingestion.py          # Discovery + Worker Lambdas, EventBridge, SQS(+DLQ), S3, Secrets
```

Install CDK deps with `pip install -e ".[cdk]"` from the repo root, then:

```bash
cd infrastructure
cdk synth                                  # synthesize the single stack
cdk deploy -c repos='["myorg/myrepo"]'     # deploy; pass the repo allowlist as context
```

Deploying builds the Lambda bundle and the MCP image, so **Docker must be running**.
After deploy, store your GitHub token in the created secret (see the `GitHubTokenSecretArn`
output) and the scheduled discovery Lambda will begin ingesting.
