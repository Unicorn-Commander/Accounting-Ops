# Contributing to Accounting-Ops

Thank you for your interest in contributing! Accounting-Ops is an open-core
project: the code in this repository is licensed under the
**Apache License 2.0**. We ask every contributor to sign off on the
**Developer Certificate of Origin (DCO)**.

## Ground rules

- Be excellent to each other. See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
- Work on a branch and open a Pull Request. **Never force-push shared history.**
- Never commit secrets — no `.env` files, API keys, private keys, or real
  financial data. CI runs `gitleaks` and `trufflehog`; a hit fails the build.
- This project handles people's financial records. Prefer correctness and
  provenance over cleverness. Do not add code that fabricates figures it cannot
  trace to a source.

## Branch conventions

| Pattern | Use |
|---|---|
| `main` | Protected integration branch. Do not push directly. |
| `dev/<agent-or-user>/<task-id>-<short-slug>` | Feature / fix work (e.g. `dev/grok/t8e387a12-community-prepublish`). |
| `docs/<slug>` | Documentation-only changes. |
| `chore/<slug>` | Tooling, deps, non-user-facing maintenance. |
| `agents/<name>` | Agent prompt / specialist packages. |

Guidelines:

- Branch from an up-to-date `origin/main` (or the base named in the task).
- Prefer **fresh worktrees** over dirty shared checkouts when parallel agents
  are active.
- Keep commits focused; one logical change per commit when practical.
- **Do not rewrite published history** (`git push --force` to shared branches is
  forbidden). Secret hygiene fixes land as forward commits or coordinated
  orphan-snapshot work — never ad-hoc history rewrites.
- Open a PR against `main`. Title/body should name the task id when one exists.

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

## Source file license headers

New or substantially rewritten source files should carry a short Apache-2.0 header at
the top (after any shebang). Entry points already use this form:

```python
# Copyright (C) 2026 Magic Unicorn Unconventional Technology & Stuff Inc
# and the Accounting-Ops contributors.
# SPDX-License-Identifier: Apache-2.0
```

For TypeScript / JavaScript, use the same text with `//` comments. The
repository license of record is still [LICENSE](LICENSE) + [NOTICE](NOTICE);
headers make provenance obvious in file-level excerpts and downstream forks.
A full-tree header retrofit is not required for every historical module before
opening a PR — add headers when you touch a file's top or create a new one.

## Enterprise modules are outside this repository

This repository is the **Community Edition**. The following live outside the
community tree (or are gated off when `AO_EDITION=community`) and must **not**
be contributed here as if they were free open-source surface:

- Multi-tenant firm / multi-client CPA console and cross-client work inbox
- Hosted-frontier billing, smart-inference credit gates, and suite entitlement
- Brigade / cross-app federation clients, discovery manifests, and M2M seams
- Platform org resolution and uc-registry workspace mapping
- Related commercial-only packages distributed under a separate license

See [COMMUNITY_MANIFEST.md](COMMUNITY_MANIFEST.md) for the path-level
COMMUNITY / ENTANGLED / ENTERPRISE-HOLD classification, and [NOTICE](NOTICE)
for the licence statement. If a change only makes sense for enterprise
hosting or suite federation, discuss it with the maintainers rather than
landing it in Community paths.

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

Contributions are made under the Apache License 2.0, the same licence as the
repository (section 5). By opening a pull request you confirm you wrote the
change or have the right to submit it under that licence.
