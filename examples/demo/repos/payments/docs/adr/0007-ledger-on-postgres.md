# ADR 0007: Store ledger balances in PostgreSQL

Status: Accepted (2024-03-12)

## Context

ledger-service needs transactional, double-entry writes with strong consistency.
We evaluated DynamoDB and PostgreSQL.

## Decision

ledger-service stores balances and journal entries in PostgreSQL, in the shared
payments-db instance, using serializable transactions.

## Consequences

payments-db becomes a dependency of both billing-api and ledger-service.
