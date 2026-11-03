# payments

The payments platform hosts two services:

- **billing-api** creates invoices and charges customers.
- **ledger-service** records balances and journal entries.

Both services are owned by team-payments. Local development uses docker-compose
(see `docker-compose.yml`); production infrastructure lives in `infra/`.

Runbook: page the payments on-call for incidents affecting invoice creation.
