"""The single Aquifer stack.

``cdk deploy`` of this one stack brings up the entire Context Lake: network, the OpenSearch
Serverless collection, the ingestion Lambdas + queue + state, and the Fargate MCP service.
The constructs are wired here; configuration flows to the runtime as ``AQUIFER_*`` env vars.

All deploy-time options are CDK context values validated by :mod:`stack_config` before
anything is synthesized; see docs/deployment.md for the full list.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_events as events
from components.ingestion import Ingestion, _retention
from components.mcp_service import McpService
from components.network import Network
from components.vector_store import VectorStore
from constructs import Construct
from stack_config import (
    DEFAULT_EMBEDDING_MODEL_ID,
    DEFAULT_INDEX,
    StackConfig,
    bedrock_model_arns,
    load_config,
)

PROJECT_ROOT = str(Path(__file__).resolve().parents[1])

# Backwards-compatible names.
DEFAULT_MODEL_ID = DEFAULT_EMBEDDING_MODEL_ID
__all__ = ["AquiferStack", "DEFAULT_INDEX", "DEFAULT_MODEL_ID"]


class AquiferStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        cfg: StackConfig = load_config(self.node.try_get_context)

        network = Network(self, "Network")

        vector_store = VectorStore(
            self,
            "VectorStore",
            vpc=network.vpc,
            security_group=network.endpoint_sg,
            collection_name="aquifer",
            standby_replicas=cfg.aoss_standby_replicas,
        )

        embed_arns = bedrock_model_arns(cfg.embedding_model_id, self.region, self.account)
        extract_arns = bedrock_model_arns(cfg.extraction_model_id, self.region, self.account)

        base_env = {
            "AQUIFER_VECTOR_STORE__ENDPOINT": vector_store.endpoint,
            "AQUIFER_VECTOR_STORE__INDEX": cfg.index,
            "AQUIFER_VECTOR_STORE__REGION": self.region,
            "AQUIFER_EMBEDDING__REGION": self.region,
            "AQUIFER_EMBEDDING__MODEL_ID": cfg.embedding_model_id,
            # Previously unset, which sent extraction to us-east-1 from any region.
            "AQUIFER_SEMANTIC_INDEX__REGION": self.region,
            "AQUIFER_SEMANTIC_INDEX__MODEL_ID": cfg.extraction_model_id,
            "AQUIFER_SEMANTIC_INDEX__ENABLED": "true" if cfg.semantic_index_enabled else "false",
            "AQUIFER_GITHUB__REPO_ALLOWLIST": Ingestion.repo_allowlist_env(cfg.repos),
        }

        ingestion = Ingestion(
            self,
            "Ingestion",
            project_root=PROJECT_ROOT,
            vpc=network.vpc,
            security_group=network.ingestion_sg,
            subnets=network.private_subnets,
            base_env=base_env,
            collection_arn=vector_store.collection_arn,
            bedrock_resources=[*embed_arns, *extract_arns],
            schedule=events.Schedule.rate(Duration.minutes(cfg.schedule_minutes)),
            worker_max_concurrency=cfg.worker_max_concurrency,
            log_retention_days=cfg.log_retention_days,
        )

        mcp = McpService(
            self,
            "Mcp",
            project_root=PROJECT_ROOT,
            dockerfile="Dockerfile.mcp",
            vpc=network.vpc,
            security_group=network.mcp_sg,
            subnets=network.private_subnets,
            base_env=base_env,
            collection_arn=vector_store.collection_arn,
            bedrock_resources=embed_arns,  # the MCP server only embeds queries
            allowed_cidrs=cfg.mcp_allowed_cidrs,
            certificate_arn=cfg.mcp_certificate_arn,
            log_retention=_retention(cfg.log_retention_days),
        )

        # Grant AOSS data access once every consuming role exists.
        vector_store.grant_data_access(
            writer_arns=ingestion.role_arns, reader_arns=[mcp.task_role.role_arn]
        )

        # Outputs carry identifiers only — never secret values.
        CfnOutput(self, "McpEndpoint", value=mcp.endpoint, description="Internal MCP NLB DNS")
        CfnOutput(self, "McpUrl", value=f"{mcp.scheme}://{mcp.endpoint}/mcp",
                  description="MCP streamable-HTTP URL (reachable only from allowed networks)")
        CfnOutput(self, "McpAuthSecretArn", value=mcp.auth_secret.secret_arn,
                  description="Secrets Manager secret holding MCP bearer tokens")
        CfnOutput(self, "CollectionEndpoint", value=vector_store.endpoint)
        CfnOutput(self, "IngestQueueUrl", value=ingestion.queue.queue_url)
        CfnOutput(self, "DeadLetterQueueUrl", value=ingestion.dlq.queue_url)
        CfnOutput(self, "DiscoveryFunctionName", value=ingestion.discovery_fn.function_name)
        CfnOutput(self, "GitHubTokenSecretArn", value=ingestion.github_token.secret_arn)
