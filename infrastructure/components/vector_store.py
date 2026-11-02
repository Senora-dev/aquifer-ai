"""The OpenSearch Serverless vector collection and its policies.

Creates a VECTORSEARCH collection reachable only through a VPC endpoint (no public access),
plus the encryption, network, and data-access policies AOSS requires. Data access is granted
via :meth:`grant_data_access` once the consuming roles exist: ingestion roles may create/update
the index and write documents; the MCP role may only describe the index and read documents.

Cost note: an OpenSearch Serverless collection bills a minimum number of OCUs while it exists,
even with no traffic (2 OCUs with standby replicas, 1 OCU without). See docs/operations.md.
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
        standby_replicas: str = "ENABLED",
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
            standby_replicas=standby_replicas,
        )
        self.collection.add_resource_dependency(encryption_policy)
        self.collection.add_resource_dependency(network_policy)

    @property
    def endpoint(self) -> str:
        return self.collection.attr_collection_endpoint

    @property
    def collection_arn(self) -> str:
        return self.collection.attr_arn

    def grant_data_access(self, writer_arns: list[str], reader_arns: list[str]) -> None:
        """Create the AOSS data-access policy: least privilege per role."""
        stack = Stack.of(self)
        index = [f"index/{self.collection_name}/*"]
        collection = [f"collection/{self.collection_name}"]
        aoss.CfnAccessPolicy(
            self,
            "DataAccessPolicy",
            name=f"{self.collection_name}-access",
            type="data",
            policy=stack.to_json_string(
                [
                    {
                        "Description": "Ingestion: manage the index and write documents",
                        "Rules": [
                            {
                                "ResourceType": "index",
                                "Resource": index,
                                "Permission": [
                                    "aoss:CreateIndex",
                                    "aoss:DescribeIndex",
                                    "aoss:UpdateIndex",
                                    "aoss:ReadDocument",
                                    "aoss:WriteDocument",
                                ],
                            },
                            {
                                "ResourceType": "collection",
                                "Resource": collection,
                                "Permission": ["aoss:DescribeCollectionItems"],
                            },
                        ],
                        "Principal": writer_arns,
                    },
                    {
                        "Description": "MCP server: read-only",
                        "Rules": [
                            {
                                "ResourceType": "index",
                                "Resource": index,
                                "Permission": ["aoss:DescribeIndex", "aoss:ReadDocument"],
                            },
                        ],
                        "Principal": reader_arns,
                    },
                ]
            ),
        )
