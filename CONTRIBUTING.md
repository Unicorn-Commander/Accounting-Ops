# Contributing to Accounting-Ops

Thank you for your interest in contributing! Accounting-Ops is an open-core
project: the Community Edition in this repository is licensed under
**AGPL-3.0-or-later**, and Magic Unicorn LLC additionally offers the project under
a commercial license. To keep that dual-licensing model working, we ask every
contributor to sign off on the **Developer Certificate of Origin (DCO)**.

## Ground rules

- Be excellent to each other. See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
- Work on a branch and open a Pull Request. **Never force-push shared history.**
- Never commit secrets — no `.env` files, API keys, private keys, or real
  financial data. CI runs `gitleaks` and `trufflehog`; a hit fails the build.
- This project handles people's financial records. Prefer correctness and
  provenance over cleverness. Do not add code that fabricates figures it cannot
  trace to a source.

## Developer Certificate of Origin (DCO) — required

We do **not** use a CLA. Instead, every commit must be signed off under the DCO.
Adding a `Signed-off-by` line certifies that you wrote the patch or otherwise
have the right to submit it under the project's license (full text below). Sign
off by committing with `-s`:

```bash
git commit -s -m "your message"
```

This appends a line to your commit message:

```
Signed-off-by: Your Name <your.email@example.com>
```

Use your real name and a reachable email. Pull requests whose commits are not
signed off will be asked to amend before merge.

### Developer Certificate of Origin 1.1

```
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.


Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

## Development setup

See the [README](README.md) QUICKSTART for the self-host development flow
(`docker compose -f docker-compose.selfhost.yml up`). In short: bring up Postgres,
the app, and the web UI; log in with local auth; provision a workspace; and you're
ready to categorize a transaction and render a report — with no external service
dependency.

## Reporting security issues

Please do **not** open a public issue for security vulnerabilities. See
[SECURITY.md](SECURITY.md) for private disclosure instructions.

## Licensing of your contribution

By submitting a contribution with a DCO sign-off, you agree that your
contribution is licensed under AGPL-3.0-or-later for the Community Edition, and
you affirm the certifications in the DCO above — which include that the work may
be redistributed consistent with the project's license(s). This preserves Magic
Unicorn LLC's ability to offer the project under both the AGPL and a commercial
license.
