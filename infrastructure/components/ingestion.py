"""Ingestion: the scheduled discovery Lambda, the SQS-driven worker Lambda, and their state.

EventBridge triggers discovery on a schedule; discovery fans fetch jobs onto the queue; the
worker consumes them, extracts, embeds, and indexes — re-enqueuing successor pages so a backfill
is just many short invocations. Both Lambdas run in private subnets and are packaged from the
repo via Docker bundling (Docker is required at deploy time).

Operational defaults:

- SQS visibility timeout is 6x the worker timeout (AWS guidance for Lambda event sources), so a
  slow invocation is never redelivered while still running.
- After 5 failed receives a job moves to the DLQ (kept 14 days); a CloudWatch alarm fires when
  the DLQ is non-empty.
- Worker concurrency is capped (``max_concurrency``) to bound GitHub and Bedrock request rates.
- Log groups have a finite retention.
"""

from __future__ import annotations

import json

from aws_cdk import BundlingOptions, Duration, RemovalPolicy
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_sqs as sqs
from aws_cdk.aws_lambda_event_sources import SqsEventSource
from constructs import Construct

_WORKER_TIMEOUT = Duration.minutes(5)
# AWS recommends a visibility timeout of at least 6x the function timeout for SQS sources.
_VISIBILITY_TIMEOUT = Duration.minutes(30)


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
        bedrock_resources: list[str],
        schedule: events.Schedule | None = None,
        worker_max_concurrency: int = 5,
        log_retention_days: int = 30,
    ) -> None:
        super().__init__(scope, construct_id)

        self.dlq = sqs.Queue(
            self,
            "Dlq",
            retention_period=Duration.days(14),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
        )
        self.queue = sqs.Queue(
            self,
            "Queue",
            visibility_timeout=_VISIBILITY_TIMEOUT,
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=5, queue=self.dlq),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
        )
        self.state_bucket = s3.Bucket(
            self,
            "State",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            # Only watermarks live here; removing the stack removes them.
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        self.github_token = secretsmanager.Secret(
            self,
            "GitHubToken",
            description="Aquifer GitHub access token (replace the generated placeholder)",
        )

        env = {
            **base_env,
            "AQUIFER_INGESTION__QUEUE_URL": self.queue.queue_url,
            "AQUIFER_INGESTION__STATE_BUCKET": self.state_bucket.bucket_name,
            "AQUIFER_GITHUB__TOKEN_SECRET": self.github_token.secret_arn,
            # Semantic indexing runs in the before_ingest hook (deterministic parsers + LLM).
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
                ".venv-*",
                ".git",
                ".github",
                "infrastructure",
                "tests",
                "examples",
                "docs",
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

        def log_group(name: str) -> logs.LogGroup:
            return logs.LogGroup(
                self,
                f"{name}Logs",
                retention=_retention(log_retention_days),
                removal_policy=RemovalPolicy.DESTROY,
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
            log_group=log_group("Discovery"),
            **common,
        )
        self.worker_fn = lambda_.Function(
            self,
            "Worker",
            handler="aquifer.ingestion.worker_handler.handler",
            timeout=_WORKER_TIMEOUT,
            log_group=log_group("Worker"),
            **common,
        )
        self.worker_fn.add_event_source(
            SqsEventSource(self.queue, batch_size=1, max_concurrency=worker_max_concurrency)
        )

        # --- permissions (least privilege) --------------------------------
        self.queue.grant_send_messages(self.discovery_fn)
        self.queue.grant_consume_messages(self.worker_fn)
        self.queue.grant_send_messages(self.worker_fn)  # successor pages, rate-limit requeues
        self.state_bucket.grant_read_write(self.discovery_fn)
        self.github_token.grant_read(self.worker_fn)
        self.github_token.grant_read(self.discovery_fn)  # credential check before fan-out

        self.worker_fn.add_to_role_policy(
            iam.PolicyStatement(actions=["bedrock:InvokeModel"], resources=bedrock_resources)
        )
        aoss_access = iam.PolicyStatement(actions=["aoss:APIAccessAll"],
                                          resources=[collection_arn])
        self.worker_fn.add_to_role_policy(aoss_access)
        self.discovery_fn.add_to_role_policy(aoss_access)  # purge of de-listed repositories

        events.Rule(
            self,
            "Schedule",
            schedule=schedule or events.Schedule.rate(Duration.minutes(15)),
            targets=[targets.LambdaFunction(self.discovery_fn)],
        )

        # --- observability ------------------------------------------------
        self.dlq_alarm = cloudwatch.Alarm(
            self,
            "DlqNotEmpty",
            alarm_description="Aquifer ingestion jobs failed permanently (see the DLQ)",
            metric=self.dlq.metric_approximate_number_of_messages_visible(
                period=Duration.minutes(5)
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        self.worker_errors_alarm = cloudwatch.Alarm(
            self,
            "WorkerErrors",
            alarm_description="Aquifer worker Lambda errors",
            metric=self.worker_fn.metric_errors(period=Duration.minutes(15)),
            threshold=5,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

    @property
    def role_arns(self) -> list[str]:
        return [self.discovery_fn.role.role_arn, self.worker_fn.role.role_arn]

    @staticmethod
    def repo_allowlist_env(repos: list[str]) -> str:
        # pydantic-settings parses list[str] env values as JSON.
        return json.dumps(repos)


def _retention(days: int) -> logs.RetentionDays:
    """Nearest supported CloudWatch retention at or above ``days``."""
    supported = [
        (1, logs.RetentionDays.ONE_DAY),
        (3, logs.RetentionDays.THREE_DAYS),
        (7, logs.RetentionDays.ONE_WEEK),
        (14, logs.RetentionDays.TWO_WEEKS),
        (30, logs.RetentionDays.ONE_MONTH),
        (60, logs.RetentionDays.TWO_MONTHS),
        (90, logs.RetentionDays.THREE_MONTHS),
        (180, logs.RetentionDays.SIX_MONTHS),
        (365, logs.RetentionDays.ONE_YEAR),
        (3653, logs.RetentionDays.TEN_YEARS),
    ]
    for value, member in supported:
        if value >= days:
            return member
    return supported[-1][1]
