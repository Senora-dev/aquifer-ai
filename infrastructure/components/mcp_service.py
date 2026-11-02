"""The MCP server: a Fargate service behind an internal Network Load Balancer.

The service runs the image built from ``Dockerfile.mcp``. The internal NLB has its own security
group admitting only the VPC CIDR plus any ``mcp_allowed_cidrs``; tasks accept traffic only from
that load balancer. Every request must carry a bearer token; the
token map is generated into Secrets Manager at deploy time and injected into the container as a
secret (never as plaintext in the task definition or in stack outputs).

The task role may invoke only the embedding model and has read-only data access to the
collection (granted by the vector-store construct).
"""

from __future__ import annotations

from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

_CONTAINER_NAME = "mcp"
_CONTAINER_PORT = 8080


class McpService(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        project_root: str,
        dockerfile: str,
        vpc: ec2.IVpc,
        security_group: ec2.ISecurityGroup,
        subnets: ec2.SubnetSelection,
        base_env: dict[str, str],
        collection_arn: str,
        bedrock_resources: list[str],
        allowed_cidrs: list[str] | None = None,
        certificate_arn: str | None = None,
        log_retention: logs.RetentionDays = logs.RetentionDays.ONE_MONTH,
    ) -> None:
        super().__init__(scope, construct_id)

        # {"default": "<48 random chars>"} — add more named tokens (one per agent/team) by
        # editing the secret; the principal name is recorded in audit logs.
        self.auth_secret = secretsmanager.Secret(
            self,
            "AuthTokens",
            description="Aquifer MCP bearer tokens: JSON object {principal: token}",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template="{}",
                generate_string_key="default",
                exclude_punctuation=True,
                password_length=48,
            ),
        )

        cluster = ecs.Cluster(
            self, "Cluster", vpc=vpc, container_insights_v2=ecs.ContainerInsights.ENABLED
        )

        task_def = ecs.FargateTaskDefinition(self, "Task", cpu=512, memory_limit_mib=1024)
        task_def.add_container(
            _CONTAINER_NAME,
            image=ecs.ContainerImage.from_asset(project_root, file=dockerfile),
            environment={**base_env, "AQUIFER_MCP__PORT": str(_CONTAINER_PORT)},
            secrets={"AQUIFER_MCP__AUTH_TOKENS": ecs.Secret.from_secrets_manager(
                self.auth_secret)},
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="aquifer-mcp",
                log_group=logs.LogGroup(self, "Logs", retention=log_retention,
                                        removal_policy=RemovalPolicy.DESTROY),
            ),
            port_mappings=[ecs.PortMapping(container_port=_CONTAINER_PORT)],
            readonly_root_filesystem=True,
        )
        self.task_role = task_def.task_role
        self.task_role.add_to_principal_policy(
            iam.PolicyStatement(actions=["bedrock:InvokeModel"], resources=bedrock_resources)
        )
        self.task_role.add_to_principal_policy(
            iam.PolicyStatement(actions=["aoss:APIAccessAll"], resources=[collection_arn])
        )

        # Client filtering happens on the load balancer's own security group: with IP targets,
        # NLB does not preserve client IPs by default, so rules on the task security group
        # would only ever see the NLB. Tasks accept traffic solely from the NLB.
        self.nlb_sg = ec2.SecurityGroup(
            self, "NlbSecurityGroup", vpc=vpc, description="Aquifer MCP load balancer",
            allow_all_outbound=False,
        )
        listener_port = 443 if certificate_arn else 80
        for cidr in [vpc.vpc_cidr_block, *(allowed_cidrs or [])]:
            self.nlb_sg.add_ingress_rule(
                ec2.Peer.ipv4(cidr), ec2.Port.tcp(listener_port), "MCP clients"
            )
        self.nlb_sg.add_egress_rule(
            security_group, ec2.Port.tcp(_CONTAINER_PORT), "to MCP tasks (traffic + health checks)"
        )
        security_group.add_ingress_rule(
            self.nlb_sg, ec2.Port.tcp(_CONTAINER_PORT), "from the MCP load balancer only"
        )

        self.service = ecs.FargateService(
            self,
            "Service",
            cluster=cluster,
            task_definition=task_def,
            desired_count=1,
            security_groups=[security_group],
            vpc_subnets=subnets,
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
        )

        self.nlb = elbv2.NetworkLoadBalancer(
            self, "Nlb", vpc=vpc, internet_facing=False, vpc_subnets=subnets,
            security_groups=[self.nlb_sg],
        )
        if certificate_arn:
            self.scheme, port = "https", 443
            listener = self.nlb.add_listener(
                "Listener",
                port=port,
                protocol=elbv2.Protocol.TLS,
                certificates=[elbv2.ListenerCertificate.from_certificate_manager(
                    acm.Certificate.from_certificate_arn(self, "Cert", certificate_arn))],
                ssl_policy=elbv2.SslPolicy.RECOMMENDED_TLS,
            )
        else:
            self.scheme, port = "http", 80
            listener = self.nlb.add_listener("Listener", port=port)
        listener.add_targets(
            "McpTargets",
            port=_CONTAINER_PORT,
            protocol=elbv2.Protocol.TCP,
            targets=[
                self.service.load_balancer_target(
                    container_name=_CONTAINER_NAME, container_port=_CONTAINER_PORT
                )
            ],
            health_check=elbv2.HealthCheck(
                interval=Duration.seconds(30), healthy_threshold_count=2
            ),
        )

    @property
    def endpoint(self) -> str:
        return self.nlb.load_balancer_dns_name
