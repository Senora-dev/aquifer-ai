# ADR 0002: One SQS queue per domain

Status: Accepted (2023-11-02)

## Decision

Each domain publishes events to its own SQS queue created from the shared
sqs-queue Terraform module in acme/terraform-modules. Queues are not shared
across domains.
