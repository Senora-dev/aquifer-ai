"""The OpenSearch Serverless vector collection and its policies.

Creates a VECTORSEARCH collection reachable only through a VPC endpoint (no public access),
plus the encryption, network, and data-access policies AOSS requires. Data-access is granted
to the ingestion and MCP roles via :meth:`grant_data_access`, called once their roles exist.
"""

from __future__ import annotations

from aws_cdk import Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_opensearchserverless as aoss
from constructs import Construct


class VectorStore(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc: ec2.IVpc,
        security_group: ec2.ISecurityGroup,
        collection_name: str = "aquifer",
    ) -> None:
        super().__init__(scope, construct_id)
        self.collection_name = collection_name
        stack = Stack.of(self)

        # Private connectivity to the collection, via the VPC's private subnets.
        self.vpc_endpoint = aoss.CfnVpcEndpoint(
            self,
            "VpcEndpoint",
            name=f"{collection_name}-vpce",
            vpc_id=vpc.vpc_id,
            subnet_ids=[subnet.subnet_id for subnet in vpc.private_subnets],
            security_group_ids=[security_group.security_group_id],
        )

        encryption_policy = aoss.CfnSecurityPolicy(
            self,
            "EncryptionPolicy",
            name=f"{collection_name}-enc",
            type="encryption",
            policy=stack.to_json_string(
                {
                    "Rules": [
                        {
                            "ResourceType": "collection",
                            "Resource": [f"collection/{collection_name}"],
                        }
                    ],
                    "AWSOwnedKey": True,
                }
            ),
        )

        network_policy = aoss.CfnSecurityPolicy(
            self,
            "NetworkPolicy",
            name=f"{collection_name}-net",
            type="network",
            policy=stack.to_json_string(
                [
                    {
                        "Rules": [
                            {
                                "ResourceType": "collection",
                                "Resource": [f"collection/{collection_name}"],
                            }
                        ],
                        "AllowFromPublic": False,
                        "SourceVPCEs": [self.vpc_endpoint.attr_id],
                    }
                ]
            ),
        )

        self.collection = aoss.CfnCollection(
            self,
            "Collection",
            name=collection_name,
            type="VECTORSEARCH",
            description="Aquifer Context Lake vector collection",
        )
        self.collection.add_dependency(encryption_policy)
        self.collection.add_dependency(network_policy)

    @property
    def endpoint(self) -> str:
        return self.collection.attr_collection_endpoint

    @property
    def collection_arn(self) -> str:
        return self.collection.attr_arn

    def grant_data_access(self, principal_arns: list[str]) -> None:
        """Create the AOSS data-access policy granting index access to the given roles."""
        stack = Stack.of(self)
        aoss.CfnAccessPolicy(
            self,
            "DataAccessPolicy",
            name=f"{self.collection_name}-access",
            type="data",
            policy=stack.to_json_string(
                [
                    {
                        "Rules": [
                            {
                                "ResourceType": "index",
                                "Resource": [f"index/{self.collection_name}/*"],
                                "Permission": ["aoss:*"],
                            },
                            {
                                "ResourceType": "collection",
                                "Resource": [f"collection/{self.collection_name}"],
                                "Permission": ["aoss:*"],
                            },
                        ],
                        "Principal": principal_arns,
                    }
                ]
            ),
        )
