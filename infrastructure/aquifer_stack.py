"""The single Aquifer stack.

``cdk deploy`` of this one stack brings up the entire Context Lake: network, the OpenSearch
Serverless collection, the ingestion Lambdas + queue + state, and the Fargate MCP service.
The constructs are wired here; configuration flows to the runtime as ``AQUIFER_*`` env vars.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import CfnOutput, Stack
from components.ingestion import Ingestion
from components.mcp_service import McpService
from components.network import Network
from components.vector_store import VectorStore
from constructs import Construct

PROJECT_ROOT = str(Path(__file__).resolve().parents[1])

# Defaults; override via CDK context (e.g. -c repos='["myorg/myrepo"]').
DEFAULT_INDEX = "aquifer-context"
DEFAULT_MODEL_ID = "amazon.titan-embed-text-v2:0"


class AquiferStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        repos = self.node.try_get_context("repos") or []
        if isinstance(repos, str):
            repos = [repos]
        index = self.node.try_get_context("index") or DEFAULT_INDEX
        model_id = self.node.try_get_context("model_id") or DEFAULT_MODEL_ID

        network = Network(self, "Network")

        vector_store = VectorStore(
            self,
            "VectorStore",
            vpc=network.vpc,
            security_group=network.app_sg,
            collection_name="aquifer",
        )

        base_env = {
            "AQUIFER_VECTOR_STORE__ENDPOINT": vector_store.endpoint,
            "AQUIFER_VECTOR_STORE__INDEX": index,
            "AQUIFER_VECTOR_STORE__REGION": self.region,
            "AQUIFER_EMBEDDING__REGION": self.region,
            "AQUIFER_EMBEDDING__MODEL_ID": model_id,
            "AQUIFER_GITHUB__REPO_ALLOWLIST": Ingestion.repo_allowlist_env(repos),
        }

        ingestion = Ingestion(
            self,
            "Ingestion",
            project_root=PROJECT_ROOT,
            vpc=network.vpc,
            security_group=network.app_sg,
            subnets=network.private_subnets,
            base_env=base_env,
            collection_arn=vector_store.collection_arn,
        )

        mcp = McpService(
            self,
            "Mcp",
            project_root=PROJECT_ROOT,
            dockerfile="Dockerfile.mcp",
            vpc=network.vpc,
            security_group=network.app_sg,
            subnets=network.private_subnets,
            base_env=base_env,
            collection_arn=vector_store.collection_arn,
        )

        # Grant AOSS data access once every consuming role exists.
        vector_store.grant_data_access([*ingestion.role_arns, mcp.task_role.role_arn])

        CfnOutput(self, "McpEndpoint", value=mcp.endpoint, description="Internal MCP NLB DNS")
        CfnOutput(self, "CollectionEndpoint", value=vector_store.endpoint)
        CfnOutput(self, "IngestQueueUrl", value=ingestion.queue.queue_url)
        CfnOutput(self, "GitHubTokenSecretArn", value=ingestion.github_token.secret_arn)
