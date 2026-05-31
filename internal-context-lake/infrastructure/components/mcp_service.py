"""The MCP server: a Fargate service behind an internal Network Load Balancer.

The service runs the image built from ``Dockerfile.mcp`` and is reachable only from within the
VPC (the NLB is internal). Its task role is granted Bedrock invoke and AOSS access so the
``search_context`` tool can embed queries and run k-NN search.
"""

from __future__ import annotations

from aws_cdk import Duration
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
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
    ) -> None:
        super().__init__(scope, construct_id)

        cluster = ecs.Cluster(self, "Cluster", vpc=vpc, container_insights=True)

        task_def = ecs.FargateTaskDefinition(self, "Task", cpu=512, memory_limit_mib=1024)
        task_def.add_container(
            _CONTAINER_NAME,
            image=ecs.ContainerImage.from_asset(project_root, file=dockerfile),
            environment={**base_env, "AQUIFER_MCP__PORT": str(_CONTAINER_PORT)},
            logging=ecs.LogDrivers.aws_logs(stream_prefix="aquifer-mcp"),
            port_mappings=[ecs.PortMapping(container_port=_CONTAINER_PORT)],
        )
        self.task_role = task_def.task_role
        self.task_role.add_to_principal_policy(
            iam.PolicyStatement(actions=["bedrock:InvokeModel"], resources=["*"])
        )
        self.task_role.add_to_principal_policy(
            iam.PolicyStatement(actions=["aoss:APIAccessAll"], resources=[collection_arn])
        )

        # Allow the NLB (which has no security group) to reach tasks on the container port.
        security_group.add_ingress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block),
            ec2.Port.tcp(_CONTAINER_PORT),
            "MCP traffic from within the VPC",
        )

        self.service = ecs.FargateService(
            self,
            "Service",
            cluster=cluster,
            task_definition=task_def,
            desired_count=1,
            security_groups=[security_group],
            vpc_subnets=subnets,
        )

        self.nlb = elbv2.NetworkLoadBalancer(
            self, "Nlb", vpc=vpc, internet_facing=False, vpc_subnets=subnets
        )
        listener = self.nlb.add_listener("Listener", port=80)
        listener.add_targets(
            "McpTargets",
            port=_CONTAINER_PORT,
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
