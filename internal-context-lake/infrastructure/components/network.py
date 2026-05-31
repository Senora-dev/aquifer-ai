"""Networking: the VPC and the private connectivity all other constructs share.

Everything runs in private subnets. A single NAT gateway provides egress to GitHub and ECR;
Bedrock, Secrets Manager, SQS, and S3 are reached over VPC endpoints so that traffic — and all
ingested context — stays on the AWS network rather than traversing the internet.
"""

from __future__ import annotations

from aws_cdk import aws_ec2 as ec2
from constructs import Construct


class Network(Construct):
    def __init__(self, scope: Construct, construct_id: str) -> None:
        super().__init__(scope, construct_id)

        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    name="private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
            ],
        )

        # Shared security group for the Lambdas, the Fargate tasks, and the VPC endpoints.
        self.app_sg = ec2.SecurityGroup(
            self,
            "AppSecurityGroup",
            vpc=self.vpc,
            description="Aquifer application components",
            allow_all_outbound=True,
        )

        # Keep Bedrock / Secrets / SQS traffic on PrivateLink.
        for name, service in (
            ("BedrockRuntime", ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME),
            ("SecretsManager", ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER),
            ("Sqs", ec2.InterfaceVpcEndpointAwsService.SQS),
        ):
            self.vpc.add_interface_endpoint(
                name, service=service, security_groups=[self.app_sg]
            )

        # S3 (Lambda state bucket) over a free gateway endpoint.
        self.vpc.add_gateway_endpoint(
            "S3", service=ec2.GatewayVpcEndpointAwsService.S3
        )

    @property
    def private_subnets(self) -> ec2.SubnetSelection:
        return ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)
