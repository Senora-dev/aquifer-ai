# Security policy

## Reporting a vulnerability

Please report vulnerabilities **privately**, not in a public issue or pull request:

- Use GitHub's **"Report a vulnerability"** button on this repository's Security tab, or
- contact the maintainers through [Senora.dev/contact](https://Senora.dev/contact) and ask for a
  private channel.

Include the affected version or commit, a description, and reproduction steps if possible. Do
not include real credentials or private data. We will acknowledge the report and keep you
informed as we investigate.

## Supported versions

Security fixes are made on `main` and included in the next release.

## Scope and security model

The deployed security model — authentication, the single trust domain, network boundaries,
least-privilege IAM, and known limitations — is documented in
[docs/security.md](docs/security.md). Please read it before reporting behavior that is a
documented limitation (for example, per-user source permissions, which are not provided).
