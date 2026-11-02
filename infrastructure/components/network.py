"""Networking: the VPC and the private connectivity all other constructs share.

Everything runs in private subnets. A single NAT gateway provides HTTPS egress to GitHub (and to
ECR / CloudWatch Logs, which have no endpoints here to avoid their fixed hourly cost); Bedrock,
Secrets Manager and SQS are reached over interface endpoints and S3 over a free gateway
endpoint, so that traffic — and all ingested context sent to Bedrock — stays on the AWS network.

Security groups are split by role:

- ``endpoint_sg``: interface endpoints and the OpenSearch Serverless endpoint; accepts HTTPS
  from inside the VPC only.
- ``ingestion_sg``: the Lambdas; no inbound; outbound TCP 443 only.
- ``mcp_sg``: the Fargate MCP tasks; inbound only on the container port (added by the MCP
  construct); outbound TCP 443 only.
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

        self.endpoint_sg = self._sg("EndpointSecurityGroup", "Aquifer VPC endpoints")
        self.endpoint_sg.add_ingress_rule(
            ec2.Peer.ipv4(self.vpc.vpc_cidr_block), ec2.Port.tcp(443), "HTTPS from the VPC"
        )
        self.ingestion_sg = self._sg("IngestionSecurityGroup", "Aquifer ingestion Lambdas")
        self.mcp_sg = self._sg("McpSecurityGroup", "Aquifer MCP server tasks")
        for sg in (self.ingestion_sg, self.mcp_sg):
            sg.add_egress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "HTTPS only")

        # Keep Bedrock / Secrets / SQS traffic on PrivateLink.
        for name, service in (
            ("BedrockRuntime", ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME),
            ("SecretsManager", ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER),
            ("Sqs", ec2.InterfaceVpcEndpointAwsService.SQS),
        ):
            self.vpc.add_interface_endpoint(
                name, service=service, security_groups=[self.endpoint_sg], open=False
            )

        # S3 (Lambda state bucket, ECR image layers) over a free gateway endpoint.
        self.vpc.add_gateway_endpoint("S3", service=ec2.GatewayVpcEndpointAwsService.S3)

    def _sg(self, construct_id: str, description: str) -> ec2.SecurityGroup:
        return ec2.SecurityGroup(
            self, construct_id, vpc=self.vpc, description=description, allow_all_outbound=False
        )

    @property
    def app_sg(self) -> ec2.SecurityGroup:
        """Backwards-compatible alias (the original single shared group)."""
        return self.ingestion_sg

    @property
    def private_subnets(self) -> ec2.SubnetSelection:
        return ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)
