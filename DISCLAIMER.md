# Disclaimer — read this before you use Accounting-Ops

**Accounting-Ops is software, not a licensed professional. It does not give you
tax, legal, accounting, or financial advice, and using it creates no
professional relationship with anyone.**

Accounting-Ops is an autonomous bookkeeping and tax-preparation assistant. It can
categorize transactions, maintain a general ledger, draft financial statements,
and assemble tax-form packages. It is a tool that helps *you* do that work. It is
**not** a Certified Public Accountant (CPA), an Enrolled Agent (EA), a tax
preparer, an attorney, or a fiduciary, and it must not be treated as one.

## Your responsibility

- **You alone are responsible** for everything you file, sign, pay, submit, or
  rely on. Read every figure and every document the software produces and verify
  it against your own source records before acting on it.
- The software can be wrong. Models make mistakes, source data can be
  incomplete, and rules change. **Do not file, pay, or sign anything you have not
  independently checked.**
- For anything with legal or financial consequence — tax returns, filings with a
  government agency, financial statements relied on by a third party — **consult
  a qualified, licensed professional** (a CPA, EA, or attorney) licensed in your
  jurisdiction.

## What the Community Edition does *not* do

To keep the free, self-hostable Community Edition safe to run, the following are
**disabled and clearly labeled** in this edition:

- **No electronic filing.** Accounting-Ops generates a tax package *for your own
  records*. It does **not** transmit anything to the IRS, a state portal, or any
  other authority. IRS e-file (MeF) requires an EFIN and an authorized
  transmitter; that path is not enabled here.
- **No tax payments.** It does not connect to EFTPS or move money to any tax
  authority.
- **No electronic signature / e-sign submission** of filings.
- **No trades, transfers, or payments** are ever executed on your behalf.

All generated tax documents are watermarked as **drafts / "do not file"** and
carry a provenance guard: the software refuses to fabricate figures it cannot
trace to a source (it returns an error rather than inventing a number).

## No warranty

This program is distributed under the GNU Affero General Public License v3
**WITHOUT ANY WARRANTY**; without even the implied warranty of MERCHANTABILITY or
FITNESS FOR A PARTICULAR PURPOSE. See the [LICENSE](LICENSE) file. To the maximum
extent permitted by law, Magic Unicorn Unconventional Technology & Stuff Inc
and the contributors are not liable
for any claim, damages, taxes, penalties, interest, or other liability arising
from your use of this software.

## Your data, your machine

The Community Edition is built to run on your own hardware with your own models.
There is no telemetry by default. **Your books. Your machine. Your rules.**

> ⚠️ **Back up your master key.** Encrypted credentials are protected by a local
> master key (`secrets/accounting_ops_master_key`). If you lose it, the encrypted
> credentials it protects are unrecoverable. Back it up somewhere safe and
> separate from your database backups.

By using Accounting-Ops you acknowledge that you have read and understood this
disclaimer.
