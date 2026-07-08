# Security Policy

Accounting-Ops handles financial records. We take security seriously and
appreciate responsible disclosure.

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues,
pull requests, or discussions.**

Instead, report privately by email to **security@example.com**. If you
prefer encrypted mail, ask for a public key in your first message.

Please include, as best you can:

- A description of the vulnerability and its impact.
- Steps to reproduce (proof-of-concept, affected endpoints/versions).
- Any suggested remediation.

We will acknowledge your report within **3 business days**, keep you informed of
our progress, and aim to ship a fix or mitigation within a reasonable window
based on severity. We will credit you in the release notes unless you ask us not
to. Please give us reasonable time to remediate before any public disclosure.

## Scope

This policy covers the Accounting-Ops **Community Edition** in this repository
and its official satellite MCP components. Third-party dependencies should be
reported to their respective maintainers (but do tell us if it affects us).

## Handling secrets and financial data

- **Never** include real secrets, API keys, or real financial/PII data in an
  issue, PR, test fixture, or bug report.
- Encrypted credentials in Accounting-Ops are protected by a local master key
  (`secrets/accounting_ops_master_key`). Treat it like a root password: back it
  up securely, never commit it, and rotate credentials if you suspect exposure.
- CI enforces secret scanning (`gitleaks` + `trufflehog`) on the codebase.

## Supported versions

Until the first tagged public release, security fixes are applied to the default
branch. Once releases are tagged, the supported window will be documented here.
