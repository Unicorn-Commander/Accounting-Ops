#!/usr/bin/env python3
"""VLM extraction bake-off — synthetic document generator (LOCAL-ONLY experiment).

Renders 5 realistic-but-synthetic financial documents to PNG using PIL and
emits a ground-truth JSON sidecar for each. NO real customer data is used.

Docs:
  doc1_bank_stmt_p1   BofA-style checking statement page (8 txn rows, running bal)
  doc2_bank_stmt_p2   continuation page (7 txn rows) — tighter font / more rows
  doc3_cc_stmt        credit-card statement (9 charges, 1 payment, 1 credit)
  doc4_receipt_cafe   coffee-shop receipt (line items, tax, tip, total)
  doc5_receipt_office office-supply receipt (small font, many SKUs)

Outputs written to OUTDIR (argv[1], default ./out):
  <docid>.png  and  <docid>.truth.json
"""
from __future__ import annotations

import json
import os
import sys
from PIL import Image, ImageDraw, ImageFont

OUTDIR = sys.argv[1] if len(sys.argv) > 1 else "out"
os.makedirs(OUTDIR, exist_ok=True)

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
MONO_B = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"


def f(path, sz):
    return ImageFont.truetype(path, sz)


def save(img, docid, truth):
    img.save(os.path.join(OUTDIR, f"{docid}.png"))
    with open(os.path.join(OUTDIR, f"{docid}.truth.json"), "w") as fh:
        json.dump(truth, fh, indent=2)
    print(f"wrote {docid}.png ({img.size[0]}x{img.size[1]}) + truth")


# ---------------------------------------------------------------------------
# doc1 — checking statement page 1 (clean, generous spacing)
# ---------------------------------------------------------------------------
def doc1():
    W, H = 1000, 1300
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 70], fill=(0, 40, 104))
    d.text((40, 22), "Bank of America", font=f(FONT_B, 26), fill="white")
    d.text((40, 95), "Adv Tiered Business Checking", font=f(FONT_B, 18), fill="black")
    d.text((40, 120), "Account # 4815 ****** 2937", font=f(FONT, 14), fill="black")
    d.text((40, 140), "Statement period: 03/01/2025 - 03/31/2025", font=f(FONT, 14), fill="black")
    d.text((600, 120), "Beginning balance   $12,480.55", font=f(MONO, 13), fill="black")
    d.text((600, 140), "Ending balance      $13,191.78", font=f(MONO, 13), fill="black")

    y = 185
    d.line([40, y, W - 40, y], fill="black", width=2)
    hdr = f(MONO_B, 13)
    d.text((48, y + 8), "Date", font=hdr, fill="black")
    d.text((150, y + 8), "Description", font=hdr, fill="black")
    d.text((640, y + 8), "Amount", font=hdr, fill="black")
    d.text((820, y + 8), "Balance", font=hdr, fill="black")
    y += 34
    d.line([40, y, W - 40, y], fill="gray", width=1)

    rows = [
        ("03/03", "DEPOSIT MOBILE 0303", 2500.00, 14980.55),
        ("03/05", "AWS *AMAZON WEB SVC 800-xxx", -342.18, 14638.37),
        ("03/07", "ZELLE TO J MARTINEZ", -1200.00, 13438.37),
        ("03/12", "STAPLES STORE #1492", -86.47, 13351.90),
        ("03/15", "DEPOSIT STRIPE TRANSFER", 1875.30, 15227.20),
        ("03/19", "GOOGLE *GSUITE_mag", -54.00, 15173.20),
        ("03/24", "COMCAST BUSINESS 8000", -219.99, 14953.21),
        ("03/28", "TFR TO SAVINGS 9981", -1761.43, 13191.78),
    ]
    mono = f(MONO, 13)
    yy = y + 8
    truth_tx = []
    for dt, desc, amt, bal in rows:
        d.text((48, yy), dt, font=mono, fill="black")
        d.text((150, yy), desc, font=mono, fill="black")
        col = (180, 0, 0) if amt < 0 else (0, 120, 0)
        astr = f"{amt:,.2f}" if amt < 0 else f"+{amt:,.2f}"
        d.text((640, yy), astr, font=mono, fill=col)
        d.text((820, yy), f"{bal:,.2f}", font=mono, fill="black")
        yy += 30
        truth_tx.append({
            "date": f"2025-{dt[:2]}-{dt[3:]}",
            "description": desc,
            "amount": amt,
            "type": "debit" if amt < 0 else "credit",
        })
    save(img, "doc1_bank_stmt_p1", {
        "doc_type": "bank_statement_page",
        "synthetic": True,
        "account_last4": "2937",
        "transactions": truth_tx,
    })


# ---------------------------------------------------------------------------
# doc2 — checking statement page 2 (denser, smaller font, 7 rows)
# ---------------------------------------------------------------------------
def doc2():
    W, H = 1000, 1300
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    d.text((40, 30), "Bank of America  —  Statement continued (page 2 of 2)",
           font=f(FONT_B, 16), fill="black")
    d.text((40, 56), "Account # 4815 ****** 2937   04/01/2025 - 04/30/2025",
           font=f(FONT, 12), fill="black")

    y = 92
    d.line([40, y, W - 40, y], fill="black", width=1)
    hdr = f(MONO_B, 11)
    d.text((46, y + 6), "Date", font=hdr, fill="black")
    d.text((140, y + 6), "Description", font=hdr, fill="black")
    d.text((700, y + 6), "Amount", font=hdr, fill="black")
    d.text((860, y + 6), "Balance", font=hdr, fill="black")
    y += 26

    rows = [
        ("04/02", "INTEREST EARNED", 3.11, 13194.89),
        ("04/04", "CHECK 1043", -450.00, 12744.89),
        ("04/09", "SHELL OIL 57442891 COLA SC", -61.04, 12683.85),
        ("04/14", "ADOBE *CREATIVE CLOUD", -59.99, 12623.86),
        ("04/18", "DEPOSIT EBAY COMMERCE INC", 743.22, 13367.08),
        ("04/22", "USPS PO 4571200998", -28.60, 13338.48),
        ("04/29", "PAYROLL FEE GUSTO", -149.00, 13189.48),
    ]
    mono = f(MONO, 11)
    yy = y + 6
    truth_tx = []
    for dt, desc, amt, bal in rows:
        d.text((46, yy), dt, font=mono, fill="black")
        d.text((140, yy), desc, font=mono, fill="black")
        astr = f"{amt:,.2f}" if amt < 0 else f"+{amt:,.2f}"
        d.text((700, yy), astr, font=mono, fill="black")
        d.text((860, yy), f"{bal:,.2f}", font=mono, fill="black")
        yy += 22
        truth_tx.append({
            "date": f"2025-{dt[:2]}-{dt[3:]}",
            "description": desc,
            "amount": amt,
            "type": "debit" if amt < 0 else "credit",
        })
    save(img, "doc2_bank_stmt_p2", {
        "doc_type": "bank_statement_page",
        "synthetic": True,
        "account_last4": "2937",
        "transactions": truth_tx,
    })


# ---------------------------------------------------------------------------
# doc3 — credit-card statement (charges + payment + credit, two columns-ish)
# ---------------------------------------------------------------------------
def doc3():
    W, H = 1000, 1350
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 64], fill=(20, 20, 20))
    d.text((40, 20), "CHASE Ink Business Preferred", font=f(FONT_B, 22), fill="white")
    d.text((40, 86), "Account ending 8801   Closing date 02/28/2025",
           font=f(FONT, 13), fill="black")
    d.text((40, 108), "New balance: $2,317.64    Minimum payment due: $35.00",
           font=f(FONT_B, 13), fill="black")

    y = 150
    d.line([40, y, W - 40, y], fill="black", width=1)
    hdr = f(MONO_B, 12)
    d.text((46, y + 6), "Trans Date", font=hdr, fill="black")
    d.text((200, y + 6), "Merchant", font=hdr, fill="black")
    d.text((760, y + 6), "Amount", font=hdr, fill="black")
    y += 28

    rows = [
        ("02/01", "PAYMENT THANK YOU - WEB", -1500.00),
        ("02/03", "DIGITALOCEAN.COM", 48.00),
        ("02/05", "UNITED AIRLINES 0162388", 412.40),
        ("02/06", "MARRIOTT ATLANTA DWTN", 289.13),
        ("02/09", "GITHUB INC HTTPSGITHUB", 21.00),
        ("02/14", "STARBUCKS STORE 09912", 7.85),
        ("02/17", "FEDEX 778812334455", 34.92),
        ("02/20", "STMT CREDIT - RETURN DELL", -212.66),
        ("02/22", "OPENAI CHATGPT SUBSCR", 20.00),
        ("02/25", "ZOOM.US 888-799-9666", 79.99),
        ("02/27", "DELTA AIR 0067711223", 318.01),
    ]
    mono = f(MONO, 12)
    yy = y + 6
    truth_tx = []
    for dt, desc, amt in rows:
        d.text((46, yy), dt, font=mono, fill="black")
        d.text((200, yy), desc, font=mono, fill="black")
        astr = f"-{abs(amt):,.2f}" if amt < 0 else f"{amt:,.2f}"
        col = (0, 120, 0) if amt < 0 else "black"
        d.text((760, yy), astr, font=mono, fill=col)
        yy += 30
        # CC convention: a positive charge increases what you owe (debit-like
        # to the cardholder's expense); payment/credit reduce balance.
        truth_tx.append({
            "date": f"2025-{dt[:2]}-{dt[3:]}",
            "description": desc,
            "amount": amt,
            "type": "payment_or_credit" if amt < 0 else "charge",
        })
    save(img, "doc3_cc_stmt", {
        "doc_type": "credit_card_statement",
        "synthetic": True,
        "account_last4": "8801",
        "transactions": truth_tx,
    })


# ---------------------------------------------------------------------------
# doc4 — cafe receipt (narrow, line items + tax + tip)
# ---------------------------------------------------------------------------
def doc4():
    W, H = 460, 760
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    cx = W // 2

    def center(txt, yy, font, fill="black"):
        w = d.textlength(txt, font=font)
        d.text((cx - w / 2, yy), txt, font=font, fill=fill)

    center("THE GRIND HOUSE CAFE", 24, f(FONT_B, 20))
    center("1492 Devine Street", 50, f(FONT, 12))
    center("Columbia, SC 29205", 66, f(FONT, 12))
    center("(803) 555-0142", 82, f(FONT, 12))
    center("------------------------------", 104, f(MONO, 12))
    d.text((24, 124), "Order #00471   03/18/2025  09:14 AM", font=f(MONO, 11), fill="black")
    center("------------------------------", 144, f(MONO, 12))

    items = [
        ("2  Cold Brew 16oz", 9.50),
        ("1  Avocado Toast", 8.75),
        ("1  Almond Croissant", 4.25),
        ("1  Oat Milk Latte", 5.50),
    ]
    mono = f(MONO, 13)
    yy = 168
    for name, price in items:
        d.text((24, yy), name, font=mono, fill="black")
        ps = f"{price:,.2f}"
        d.text((W - 24 - d.textlength(ps, font=mono), yy), ps, font=mono, fill="black")
        yy += 26
    yy += 6
    center("------------------------------", yy, f(MONO, 12))
    yy += 24

    subtotal = 28.00
    tax = 1.96
    tip = 5.00
    total = 34.96
    for label, val in [("Subtotal", subtotal), ("Sales Tax 7%", tax),
                       ("Tip", tip), ("TOTAL", total)]:
        ff = f(MONO_B, 14) if label == "TOTAL" else mono
        d.text((24, yy), label, font=ff, fill="black")
        vs = f"{val:,.2f}"
        d.text((W - 24 - d.textlength(vs, font=ff), yy), vs, font=ff, fill="black")
        yy += 28
    yy += 8
    center("VISA ****1187   APPROVED", yy, f(MONO, 11))
    yy += 30
    center("Thank you!", yy, f(FONT, 13))

    save(img, "doc4_receipt_cafe", {
        "doc_type": "receipt",
        "synthetic": True,
        "vendor": "The Grind House Cafe",
        "date": "2025-03-18",
        "line_items": [{"description": n, "amount": p} for n, p in items],
        "subtotal": subtotal,
        "tax": tax,
        "tip": tip,
        "total": total,
        "transactions": [{
            "date": "2025-03-18",
            "description": "The Grind House Cafe",
            "amount": -total,
            "type": "debit",
        }],
    })


# ---------------------------------------------------------------------------
# doc5 — office-supply receipt (small font, many SKUs, harder OCR)
# ---------------------------------------------------------------------------
def doc5():
    W, H = 520, 880
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    cx = W // 2

    def center(txt, yy, font, fill="black"):
        w = d.textlength(txt, font=font)
        d.text((cx - w / 2, yy), txt, font=font, fill=fill)

    center("STAPLES", 18, f(FONT_B, 22))
    center("Store #1492  Columbia SC", 46, f(FONT, 11))
    center("Tel (803) 555-7781", 62, f(FONT, 11))
    d.text((20, 88), "CASHIER: 04  REG: 3  03/12/2025 14:22",
           font=f(MONO, 10), fill="black")
    d.line([20, 108, W - 20, 108], fill="black", width=1)

    # small font, qty x unit each, extended price — multi-column tax-prone
    items = [
        ("HAMMERMILL COPY 8.5x11 10RM", 54.99),
        ("PILOT G2 GEL PEN BLK 12PK", 13.49),
        ("POST-IT NOTES 3x3 18PK", 21.98),
        ("EXPO DRY ERASE ASST 8CT", 11.29),
        ("SCOTCH TAPE 6PK", 9.97),
        ("HP 64 BLACK INK", 24.99),
        ("DURACELL AA 16CT", 16.49),
        ("FILE FOLDERS LTR 100CT", 18.79),
    ]
    mono = f(MONO, 11)
    yy = 118
    for name, price in items:
        d.text((20, yy), name, font=mono, fill="black")
        ps = f"{price:,.2f}"
        d.text((W - 20 - d.textlength(ps, font=mono), yy), ps, font=mono, fill="black")
        yy += 20
    d.line([20, yy + 2, W - 20, yy + 2], fill="black", width=1)
    yy += 12

    subtotal = 171.99
    tax = 12.04
    total = 184.03
    for label, val in [("SUBTOTAL", subtotal), ("TAX 7.0%", tax), ("TOTAL", total)]:
        ff = f(MONO_B, 13) if label == "TOTAL" else mono
        d.text((20, yy), label, font=ff, fill="black")
        vs = f"{val:,.2f}"
        d.text((W - 20 - d.textlength(vs, font=ff), yy), vs, font=ff, fill="black")
        yy += 24
    yy += 6
    d.text((20, yy), "MASTERCARD ****6620  CHIP", font=f(MONO, 10), fill="black")
    yy += 18
    d.text((20, yy), "AUTH 047781  REF 0099281", font=f(MONO, 10), fill="black")

    save(img, "doc5_receipt_office", {
        "doc_type": "receipt",
        "synthetic": True,
        "vendor": "Staples",
        "date": "2025-03-12",
        "line_items": [{"description": n, "amount": p} for n, p in items],
        "subtotal": subtotal,
        "tax": tax,
        "total": total,
        "transactions": [{
            "date": "2025-03-12",
            "description": "Staples Store #1492",
            "amount": -total,
            "type": "debit",
        }],
    })


if __name__ == "__main__":
    doc1()
    doc2()
    doc3()
    doc4()
    doc5()
    print("DONE generating", OUTDIR)
