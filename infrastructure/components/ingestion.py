"""Ingestion: the scheduled discovery Lambda, the SQS-driven worker Lambda, and their state.

EventBridge triggers discovery on a schedule; discovery fans fetch jobs onto the queue; the
worker consumes them, embeds, and upserts — re-enqueuing successor pages so a backfill is just
many short invocations. Both Lambdas run in private subnets and are packaged from the repo via
Docker bundling (Docker is required at deploy time).
"""

from __future__ import annotations

import json

from aws_cdk import BundlingOptions, Duration
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_sqs as sqs
from aws_cdk.aws_lambda_event_sources import SqsEventSource
from constructs import Construct

# Worker timeout; SQS visibility timeout must exceed this.
_WORKER_TIMEOUT = Duration.minutes(5)


class Ingestion(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        project_root: str,
        vpc: ec2.IVpc,
        security_group: ec2.ISecurityGroup,
        subnets: ec2.SubnetSelection,
        base_env: dict[str, str],
        collection_arn: str,
        schedule: events.Schedule | None = None,
    ) -> None:
        super().__init__(scope, construct_id)

        self.dlq = sqs.Queue(self, "Dlq", retention_period=Duration.days(14))
        self.queue = sqs.Queue(
            self,
            "Queue",
            visibility_timeout=Duration.minutes(6),
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=5, queue=self.dlq),
        )
        self.state_bucket = s3.Bucket(
            self,
            "State",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
        )
        self.github_token = secretsmanager.Secret(
            self, "GitHubToken", description="Aquifer GitHub access token"
        )

        env = {
            **base_env,
            "AQUIFER_INGESTION__QUEUE_URL": self.queue.queue_url,
            "AQUIFER_INGESTION__STATE_BUCKET": self.state_bucket.bucket_name,
            "AQUIFER_GITHUB__TOKEN_SECRET": self.github_token.secret_arn,
            # Run the semantic indexer in the before_ingest hook so every item gets neutral,
            # queryable metadata before indexing. Worker InvokeModel permission already covers
            # the extraction model.
            "AQUIFER_INTERCEPTORS": json.dumps(
                ["aquifer.semantic.interceptor:SemanticIndexInterceptor"]
            ),
        }

        code = lambda_.Code.from_asset(
            project_root,
            # The bundle only needs pyproject.toml, README.md, and src/ to pip-install the
            # package. Excluding the rest keeps the asset small and avoids copying cdk.out/.venv.
            exclude=[
                ".venv",
                ".git",
                "infrastructure",
                "tests",
                "**/__pycache__",
                "dist",
                "build",
                "*.egg-info",
            ],
            bundling=BundlingOptions(
                image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                command=["bash", "-c", "pip install '.[ingestion]' -t /asset-output"],
            ),
        )

        common = {
            "runtime": lambda_.Runtime.PYTHON_3_12,
            "code": code,
            "vpc": vpc,
            "vpc_subnets": subnets,
            "security_groups": [security_group],
            "environment": env,
            "memory_size": 1024,
        }

        self.discovery_fn = lambda_.Function(
            self,
            "Discovery",
            handler="aquifer.ingestion.discovery_handler.handler",
            timeout=Duration.minutes(5),
            **common,
        )
        self.worker_fn = lambda_.Function(
            self,
            "Worker",
            handler="aquifer.ingestion.worker_handler.handler",
            timeout=_WORKER_TIMEOUT,
            **common,
        )
        self.worker_fn.add_event_source(SqsEventSource(self.queue, batch_size=1))

        # --- permissions -------------------------------------------------
        self.queue.grant_send_messages(self.discovery_fn)
        self.queue.grant_consume_messages(self.worker_fn)
        self.queue.grant_send_messages(self.worker_fn)  # re-enqueue successor pages
        self.state_bucket.grant_read_write(self.discovery_fn)
        self.github_token.grant_read(self.worker_fn)

        bedrock = iam.PolicyStatement(actions=["bedrock:InvokeModel"], resources=["*"])
        aoss_access = iam.PolicyStatement(
            actions=["aoss:APIAccessAll"], resources=[collection_arn]
        )
        self.worker_fn.add_to_role_policy(bedrock)
        self.worker_fn.add_to_role_policy(aoss_access)

        # Scheduled discovery.
        events.Rule(
            self,
            "Schedule",
            schedule=schedule or events.Schedule.rate(Duration.minutes(15)),
            targets=[targets.LambdaFunction(self.discovery_fn)],
        )

    @property
    def role_arns(self) -> list[str]:
        return [self.discovery_fn.role.role_arn, self.worker_fn.role.role_arn]

    @staticmethod
    def repo_allowlist_env(repos: list[str]) -> str:
        # pydantic-settings parses list[str] env values as JSON.
        return json.dumps(repos)
