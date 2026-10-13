"""Branded invoice HTML. One letter page. The hosted page, the PDF, and the
email all start from this document.

The look is the approved Special Projects sheet: black hero, purple and cyan
aurora, the eyepatch mark, the amount in gradient type, then the parties, the
line, and the pay box. Copy stays inside the brand voice. The legal name is
the entity's legal name.
"""
from __future__ import annotations

import base64
import html
import json
import os
import shutil
import subprocess
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

_LOGO = Path(__file__).with_name("mu-special-projects-logo.png")
_LOGO_URI: str | None = None

_DEFAULT_PRIMARY = "#b66eff"
_DEFAULT_ACCENT = "#00d4ff"
_DEFAULT_NIGHT = "#0a0a0a"


def default_logo_data_uri() -> str:
    global _LOGO_URI
    if _LOGO_URI is None:
        raw = _LOGO.read_bytes()
        _LOGO_URI = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    return _LOGO_URI


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _money(value: Any, currency: str = "USD") -> str:
    try:
        n = Decimal(str(value if value is not None else "0"))
    except Exception:
        return _esc(value)
    sym = "$" if (currency or "USD").upper() == "USD" else f"{currency} "
    neg = n < 0
    n = abs(n).quantize(Decimal("0.01"))
    whole, cents = str(n).split(".")
    grouped = f"{int(whole):,}"
    return f"{'-' if neg else ''}{sym}{grouped}.{cents}"


def _presentation(view: dict[str, Any]) -> dict[str, Any]:
    raw = view.get("presentation")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return raw if isinstance(raw, dict) else {}


def _lines(view: dict[str, Any]) -> list[dict[str, Any]]:
    rows = view.get("lines") or []
    return [row for row in rows if isinstance(row, dict)]


def render_invoice_html(view: dict[str, Any]) -> str:
    """Render one invoice. Does not post anything and does not include the design preview banner."""
    pres = _presentation(view)
    currency = str(view.get("currency") or "USD")
    primary = view.get("color_primary") or _DEFAULT_PRIMARY
    accent = view.get("color_accent") or _DEFAULT_ACCENT
    night = view.get("color_background") or _DEFAULT_NIGHT
    product = str(pres.get("product_name") or "Magic Unicorn")
    product_line = str(pres.get("product_line") or "Special Projects")
    legal = str(
        view.get("legal_name")
        or view.get("entity_legal_name")
        or "Magic Unicorn Unconventional Technology & Stuff Inc"
    )
    footer = str(view.get("footer_line") or "magicunicorn.dev · Charleston, SC")
    number = str(view.get("invoice_number") or "DRAFT")
    total = view.get("total") or view.get("balance_due") or "0"
    due_label = str(pres.get("due_label") or "on receipt")
    issued = str(view.get("issue_date") or "")
    logo = view.get("logo_url") or default_logo_data_uri()
    customer = str(view.get("customer_name") or "Client")
    email = view.get("customer_email") or ""
    address = view.get("billing_address") or view.get("customer_billing_address") or ""
    from_address_block = view.get("invoice_address") or ""
    contact_name = pres.get("contact_name") or os.environ.get("INVOICE_CONTACT_NAME") or ""
    contact_email = pres.get("contact_email") or view.get("reply_to") or view.get("from_address") or ""
    contact_phone = pres.get("contact_phone") or ""

    lines = _lines(view)
    headline = str(pres.get("headline") or (lines[0].get("description") if lines else "Services"))
    deliverables = pres.get("deliverables") if isinstance(pres.get("deliverables"), list) else []
    chips = pres.get("not_included") if isinstance(pres.get("not_included"), list) else []
    steps = pres.get("next_steps") if isinstance(pres.get("next_steps"), list) else []
    note = str(pres.get("note") or "")
    thanks = str(pres.get("thanks") or f"Thanks, {customer.split(',')[0].split(' ')[0]}.")

    subtotal = view.get("subtotal") if view.get("subtotal") is not None else total
    tax = view.get("tax_total") if view.get("tax_total") is not None else "0"
    pay_url = view.get("hosted_pay_url") or view.get("public_url") or ""
    pay_connected = bool(view.get("hosted_pay_url"))
    if pay_connected:
        pay_button = f'<a class="btn" href="{_esc(pay_url)}">Pay invoice →</a>'
        pay_detail = "ACH or card."
    elif pay_url:
        pay_button = f'<a class="btn" href="{_esc(pay_url)}">View invoice →</a>'
        pay_detail = "The pay link isn't connected yet."
    else:
        pay_button = '<span class="btn" aria-disabled="true">Pay link isn\'t connected</span>'
        pay_detail = "The pay link isn't connected yet."

    line_rows = []
    for index, line in enumerate(lines or [{"description": headline, "quantity": 1, "amount": total}]):
        desc = line.get("description") or headline
        extra = ""
        if index == 0 and note:
            extra += f'<div class="d">{_esc(note)}</div>'
        if index == 0 and deliverables:
            items = "".join(f"<li>{_esc(item)}</li>" for item in deliverables[:12])
            extra += f"<ul>{items}</ul>"
        qty = line.get("quantity") if line.get("quantity") is not None else 1
        amount = line.get("amount") if line.get("amount") is not None else total
        line_rows.append(
            "<div class=\"row\">"
            f"<div><div class=\"t\">{_esc(desc)}</div>{extra}</div>"
            f"<div class=\"r\">{_esc(_qty(qty))}</div>"
            f"<div class=\"r\"><b>{_esc(_money(amount, currency))}</b></div>"
            "</div>"
        )

    parties = [
        f'<div><div class="k">Billed to</div><div class="v"><b>{_esc(customer)}</b>'
        + (f"<br>{_esc(address).replace(chr(10), '<br>')}" if address else "")
        + (f"<br>{_esc(email)}" if email else "")
        + "</div></div>",
        f'<div><div class="k">From</div><div class="v"><b>{_esc(legal)}</b>'
        + (f"<br>{_esc(from_address_block).replace(chr(10), '<br>')}" if from_address_block else "")
        + "</div></div>",
    ]
    if contact_name:
        parties.append(
            '<div><div class="k">Your contact</div><div class="v">'
            f"<b>{_esc(contact_name)}</b>"
            + (f"<br>{_esc(contact_email)}" if contact_email else "")
            + (f"<br>{_esc(contact_phone)}" if contact_phone else "")
            + "</div></div>"
        )

    chips_html = ""
    if chips:
        chip_spans = "".join(f'<span class="chip">{_esc(chip)}</span>' for chip in chips[:8])
        chips_html = (
            '<div class="card"><h3>Not on this invoice. You pay these directly.</h3>'
            f'<div class="chips">{chip_spans}</div></div>'
        )
    steps_html = ""
    if steps:
        items = "".join(
            f"<li><span><b>{_esc(step)}</b></span></li>" for step in steps[:3]
        )
        steps_html = f'<div class="card"><h3>What happens next</h3><ol class="steps">{items}</ol></div>'
    two = ""
    if chips_html or steps_html:
        two = f'<div class="two">{chips_html}{steps_html}</div>'

    meta_bits = []
    if issued:
        meta_bits.append(f"Issued <b>{_esc(issued)}</b>")
    if pres.get("meta"):
        meta_bits.append(_esc(pres.get("meta")))
    meta = "<br>".join(meta_bits)

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Invoice · {_esc(number)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500&display=swap" rel="stylesheet">
<style>
:root{{--ink:#111114;--soft:#5b5b66;--faint:#9a9aa6;--line:#ececf1;--purple:{_esc(primary)};--cyan:{_esc(accent)};--night:{_esc(night)};--paper:#fff}}
*{{box-sizing:border-box;-webkit-print-color-adjust:exact;print-color-adjust:exact}}
@page{{size:letter;margin:0}}
html{{background:#e9e9ef}}
body{{margin:0;font:13px/1.5 Inter,-apple-system,Helvetica,Arial,sans-serif;color:var(--ink);font-feature-settings:"tnum" 1,"cv11" 1}}
.sheet{{width:8.5in;min-height:11in;margin:0 auto;background:var(--paper);position:relative;overflow:hidden;display:flex;flex-direction:column}}
.hero{{position:relative;background:var(--night);color:#fff;padding:34px 48px 30px;overflow:hidden}}
.hero:before{{content:"";position:absolute;inset:-40% -10% auto auto;width:520px;height:520px;background:radial-gradient(closest-side,color-mix(in srgb,var(--purple) 55%,transparent),transparent 70%);filter:blur(10px)}}
.hero:after{{content:"";position:absolute;left:-120px;bottom:-260px;width:520px;height:520px;background:radial-gradient(closest-side,color-mix(in srgb,var(--cyan) 35%,transparent),transparent 70%);filter:blur(10px)}}
.hero>*{{position:relative;z-index:1}}
.brandrow{{display:flex;align-items:center;justify-content:space-between;gap:16px}}
.brand{{display:flex;align-items:center;gap:14px}}
.brand img{{height:58px;width:auto}}
.brand .n1{{font-weight:800;font-size:17px;letter-spacing:-.2px}}
.brand .n2{{font-size:11px;letter-spacing:2.2px;text-transform:uppercase;color:#c9b6ff}}
.tag{{font:500 11px/1 "JetBrains Mono",Monaco,monospace;letter-spacing:1.5px;color:#bdf3ff;border:1px solid color-mix(in srgb,var(--cyan) 45%,transparent);padding:7px 10px;border-radius:999px}}
.amount{{margin-top:30px;display:flex;align-items:flex-end;justify-content:space-between;gap:24px}}
.amount .lbl{{font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#b3b3b3}}
.amount .big{{font-size:56px;font-weight:800;letter-spacing:-2px;line-height:1;margin-top:6px;background:linear-gradient(90deg,#fff 0%,#e9d9ff 55%,#bdf3ff 100%);-webkit-background-clip:text;background-clip:text;color:transparent}}
.amount .due{{font-size:14px;color:#e6e6ea;margin-top:8px}}
.amount .due b{{color:#fff}}
.meta{{text-align:right;font-size:12px;color:#cfcfd6;line-height:1.7}}
.meta b{{color:#fff;font-weight:600}}
.rule{{height:4px;background:linear-gradient(90deg,var(--purple),var(--cyan))}}
main{{padding:30px 48px 0;flex:1}}
.parties{{display:grid;grid-template-columns:repeat({len(parties)},1fr);gap:28px}}
.k{{font-size:10.5px;letter-spacing:1.6px;text-transform:uppercase;color:var(--faint);font-weight:600;margin-bottom:6px}}
.parties .v{{font-size:12.5px;color:var(--soft)}}
.parties .v b{{color:var(--ink);font-size:13.5px}}
.items{{margin-top:28px;border:1px solid var(--line);border-radius:14px;overflow:hidden}}
.items .head,.items .row{{display:grid;grid-template-columns:1fr 60px 120px;gap:16px;padding:12px 20px}}
.items .head{{background:#fafafc;font-size:10.5px;letter-spacing:1.6px;text-transform:uppercase;color:var(--faint);font-weight:600}}
.items .row{{padding:18px 20px}}
.items .t{{font-weight:700;font-size:14.5px}}
.items .d{{color:var(--soft);font-size:12.5px;margin-top:6px}}
.items ul{{margin:8px 0 0;padding-left:0;list-style:none;display:grid;grid-template-columns:1fr 1fr;gap:4px 18px;color:var(--soft);font-size:12px}}
.items li:before{{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:linear-gradient(135deg,var(--purple),var(--cyan));margin-right:8px}}
.r{{text-align:right}}
.totals{{display:flex;justify-content:flex-end;margin-top:14px}}
.totals table{{border-collapse:collapse;min-width:260px;font-size:13px}}
.totals td{{padding:5px 0}}
.totals td+td{{text-align:right}}
.totals .sum td{{border-top:2px solid var(--ink);padding-top:10px;font-size:18px;font-weight:800}}
.two{{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-top:22px}}
.card{{border:1px solid var(--line);border-radius:14px;padding:16px 18px}}
.card h3{{margin:0 0 10px;font-size:13px}}
.chips{{display:flex;flex-wrap:wrap;gap:7px}}
.chip{{font-size:11.5px;padding:6px 10px;border-radius:999px;background:#f4f4f7;color:#3a3a44}}
.steps{{margin:0;padding:0;list-style:none;counter-reset:s}}
.steps li{{counter-increment:s;display:flex;gap:10px;font-size:12px;color:var(--soft);margin-bottom:8px}}
.steps li:before{{content:counter(s);flex:0 0 20px;height:20px;border-radius:50%;display:grid;place-items:center;font-size:11px;font-weight:700;color:#fff;background:linear-gradient(135deg,var(--purple),var(--cyan))}}
.pay{{margin-top:22px;border-radius:16px;padding:18px 20px;display:flex;align-items:center;justify-content:space-between;gap:18px;background:linear-gradient(135deg,#f6efff,#eafcff);border:1px solid #e3d4ff}}
.pay .p1{{font-weight:700;font-size:14px}}
.pay .p2{{font-size:12px;color:var(--soft);margin-top:2px}}
.btn{{display:inline-block;text-decoration:none;color:#fff;font-weight:700;font-size:13.5px;padding:12px 20px;border-radius:12px;background:linear-gradient(90deg,#9b4dff,#00b8e6)}}
footer{{padding:22px 48px 26px;margin-top:24px;border-top:1px solid var(--line);display:flex;justify-content:space-between;align-items:flex-end;gap:20px;font-size:11px;color:var(--faint)}}
.thanks{{font-size:15px;font-weight:700;color:var(--ink);margin-bottom:4px}}
@media print{{html,body{{background:#fff}}.btn{{box-shadow:none}}.sheet{{margin:0;zoom:.84;width:calc(8.5in/.84);height:calc(11in/.84 - 2px);min-height:0}}}}
</style></head><body>
<div class="sheet">
 <section class="hero">
  <div class="brandrow">
   <div class="brand"><img src="{_esc(logo)}" alt="{_esc(product)} {_esc(product_line)} logo">
    <div><div class="n1">{_esc(product)}</div><div class="n2">{_esc(product_line)}</div></div></div>
   <div class="tag">INVOICE · {_esc(number)}</div>
  </div>
  <div class="amount">
   <div><div class="lbl">Amount due</div><div class="big">{_esc(_money(view.get("balance_due") if view.get("balance_due") is not None else total, currency))}</div>
    <div class="due">Due <b>{_esc(due_label)}</b> · pay by ACH or card</div></div>
   <div class="meta">{meta}</div>
  </div>
 </section>
 <div class="rule"></div>
 <main>
  <div class="parties">{''.join(parties)}</div>
  <div class="items">
   <div class="head"><div>What you're paying for</div><div class="r">Qty</div><div class="r">Amount</div></div>
   {''.join(line_rows)}
  </div>
  <div class="totals"><table>
   <tr><td>Subtotal</td><td>{_esc(_money(subtotal, currency))}</td></tr>
   <tr><td>Tax</td><td>{_esc(_money(tax, currency))}</td></tr>
   <tr class="sum"><td>Total due</td><td>{_esc(_money(total, currency))}</td></tr>
  </table></div>
  {two}
  <div class="pay"><div><div class="p1">Pay {_esc(_money(view.get("balance_due") if view.get("balance_due") is not None else total, currency))} securely</div><div class="p2">{pay_detail}</div></div>{pay_button}</div>
 </main>
 <footer>
  <div><div class="thanks">{_esc(thanks)}</div></div>
  <div style="text-align:right">{_esc(legal)}<br>{_esc(footer)}</div>
 </footer>
</div>
</body></html>"""


def _qty(value: Any) -> str:
    try:
        n = Decimal(str(value))
    except Exception:
        return str(value)
    if n == n.to_integral():
        return str(int(n))
    return f"{n.normalize()}"


def render_invoice_pdf(document_html: str) -> bytes:
    """Print the HTML to one PDF. WeasyPrint when it imports, otherwise headless Chrome.
    Raises when neither can render. The old reportlab invoice is not a fallback.
    """
    try:
        from weasyprint import HTML  # type: ignore

        return HTML(string=document_html).write_pdf()
    except Exception:
        pass

    chrome = (
        shutil.which("google-chrome")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    )
    if not chrome or not os.path.exists(chrome):
        raise RuntimeError("No HTML-to-PDF renderer is available (WeasyPrint or Chrome).")
    with tempfile.TemporaryDirectory() as tmp:
        html_path = Path(tmp) / "invoice.html"
        pdf_path = Path(tmp) / "invoice.pdf"
        html_path.write_text(document_html, encoding="utf-8")
        subprocess.run(
            [
                chrome,
                "--headless=new",
                "--disable-gpu",
                "--no-pdf-header-footer",
                f"--print-to-pdf={pdf_path}",
                html_path.as_uri(),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        return pdf_path.read_bytes()
