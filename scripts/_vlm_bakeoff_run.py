#!/usr/bin/env python3
"""VLM extraction bake-off — run local Qwen vision extraction + score (LOCAL-ONLY).

For each <docid>.png + <docid>.truth.json in OUTDIR, send the image to the
local OpenAI-compatible vision endpoint, ask for structured JSON transactions,
parse the reply, and score against ground truth.

ZERO paid API spend: hits http://localhost:8088/v1 (local llama.cpp on midboy1).

Scoring (per doc):
  - row recall:    matched_truth_rows / total_truth_rows
  - row precision: matched_extracted_rows / total_extracted_rows
  - amount exact:  of matched rows, fraction with amount correct to the cent
  - date exact:    of matched rows, fraction with date correct
  A "match" pairs an extracted row to a truth row by closest amount (abs cents)
  within $0.01 OR, failing that, by date+vendor token overlap. Greedy 1:1.

Writes vlm_bakeoff_results.json (raw + scored) to OUTDIR.
"""
from __future__ import annotations

import base64
import glob
import json
import os
import re
import sys
import time

import requests

OUTDIR = sys.argv[1] if len(sys.argv) > 1 else "out"
BASE_URL = os.environ.get("VLM_BASE_URL", "http://localhost:8088/v1")
MODEL = os.environ.get("VLM_MODEL", "Qwen3.6-35B-A3B-Vision")
TIMEOUT = int(os.environ.get("VLM_TIMEOUT", "600"))

PROMPT = """You are a financial-document data-extraction engine. The image is a \
bank statement page, credit-card statement, or a purchase receipt.

Extract EVERY transaction line you can see. For a receipt, treat the single \
purchase (the TOTAL paid) as one transaction; do not emit a row per line item.

Return ONLY a JSON object, no prose, no markdown fences, in exactly this shape:
{
  "doc_type": "bank_statement" | "credit_card_statement" | "receipt",
  "transactions": [
    {
      "date": "YYYY-MM-DD",
      "description": "merchant or memo text exactly as printed",
      "amount": <number, negative for money leaving the account / charges that \
increase what is owed, positive for deposits/credits/payments-received>,
      "type": "debit" | "credit" | "charge" | "payment_or_credit"
    }
  ]
}

Rules:
- amount is a plain number (no $ , or commas). Money OUT is negative, money IN \
is positive. On a credit-card statement, a purchase/charge is negative and a \
payment or statement-credit is positive.
- If the year is not printed, assume 2025.
- Read small fonts carefully; get every cent right.
- Do not invent rows. Do not skip rows."""


def encode(path: str) -> str:
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode()


def call_vlm(png_path: str) -> tuple[str, float, dict]:
    b64 = encode(png_path)
    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    },
                ],
            }
        ],
        "temperature": 0.0,
        "max_tokens": 2048,
    }
    t0 = time.monotonic()
    r = requests.post(
        f"{BASE_URL}/chat/completions", json=payload, timeout=TIMEOUT
    )
    dt = time.monotonic() - t0
    r.raise_for_status()
    body = r.json()
    txt = body["choices"][0]["message"]["content"]
    usage = body.get("usage", {})
    return txt, dt, usage


def parse_json(txt: str) -> dict | None:
    # strip code fences if present
    s = txt.strip()
    s = re.sub(r"^```(?:json)?", "", s).strip()
    s = re.sub(r"```$", "", s).strip()
    # find the outermost {...}
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start : i + 1])
                except json.JSONDecodeError:
                    # try a forgiving cleanup: remove trailing commas
                    cleaned = re.sub(r",\s*([}\]])", r"\1", s[start : i + 1])
                    try:
                        return json.loads(cleaned)
                    except json.JSONDecodeError:
                        return None
    return None


def cents(x) -> int | None:
    try:
        return round(float(x) * 100)
    except (TypeError, ValueError):
        return None


def tokset(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def score_doc(truth: dict, extracted: dict | None) -> dict:
    truth_tx = truth.get("transactions", [])
    ex_tx = (extracted or {}).get("transactions", []) or []
    # normalize extracted to a flat list
    ex_norm = []
    for t in ex_tx:
        if not isinstance(t, dict):
            continue
        ex_norm.append({
            "date": t.get("date"),
            "description": t.get("description") or t.get("vendor") or "",
            "amount": t.get("amount"),
            "type": t.get("type"),
            "_cents": cents(t.get("amount")),
        })

    # greedy 1:1 match: for each truth row, find unused extracted row with
    # closest |amount| within $0.01; tie-break on date/vendor token overlap.
    used = set()
    matches = []
    for ti, tr in enumerate(truth_tx):
        tc = cents(tr["amount"])
        best = None
        best_key = None
        for ei, er in enumerate(ex_norm):
            if ei in used:
                continue
            ec = er["_cents"]
            # primary: signed-amount match to the cent
            if ec is not None and tc is not None and ec == tc:
                key = (0, 0)  # perfect
            elif ec is not None and tc is not None and abs(abs(ec) - abs(tc)) == 0:
                key = (1, 0)  # right magnitude, sign flipped
            else:
                # fallback: vendor token overlap + date
                ov = len(tokset(tr["description"]) & tokset(er["description"]))
                if ov == 0:
                    continue
                date_ok = 0 if tr.get("date") == er.get("date") else 1
                key = (2, date_ok, -ov)
            if best_key is None or key < best_key:
                best_key = key
                best = ei
        if best is not None:
            used.add(best)
            er = ex_norm[best]
            tc = cents(tr["amount"])
            ec = er["_cents"]
            matches.append({
                "truth": tr,
                "extracted": er,
                "amount_exact": (ec is not None and tc is not None and ec == tc),
                "amount_magnitude_ok": (
                    ec is not None and tc is not None and abs(ec) == abs(tc)
                ),
                "date_exact": tr.get("date") == er.get("date"),
            })

    n_truth = len(truth_tx)
    n_ex = len(ex_norm)
    n_match = len(matches)
    amt_exact = sum(1 for m in matches if m["amount_exact"])
    amt_mag = sum(1 for m in matches if m["amount_magnitude_ok"])
    date_exact = sum(1 for m in matches if m["date_exact"])

    return {
        "n_truth": n_truth,
        "n_extracted": n_ex,
        "n_matched": n_match,
        "recall": n_match / n_truth if n_truth else None,
        "precision": n_match / n_ex if n_ex else None,
        "amount_exact_of_matched": amt_exact / n_match if n_match else None,
        "amount_magnitude_of_matched": amt_mag / n_match if n_match else None,
        "date_exact_of_matched": date_exact / n_match if n_match else None,
        "amount_exact_count": amt_exact,
        "matches": matches,
        "missed_truth_rows": [
            tr for tr in truth_tx
            if not any(m["truth"] is tr for m in matches)
        ],
        "unmatched_extracted_rows": [
            ex_norm[i] for i in range(n_ex) if i not in used
        ],
    }


def main():
    truths = sorted(glob.glob(os.path.join(OUTDIR, "*.truth.json")))
    results = {}
    for tp in truths:
        docid = os.path.basename(tp).replace(".truth.json", "")
        png = os.path.join(OUTDIR, f"{docid}.png")
        with open(tp) as fh:
            truth = json.load(fh)
        print(f"\n=== {docid} ===", flush=True)
        try:
            raw, dt, usage = call_vlm(png)
        except Exception as exc:
            print(f"  ERROR calling VLM: {exc}", flush=True)
            results[docid] = {"error": str(exc), "truth": truth}
            continue
        parsed = parse_json(raw)
        sc = score_doc(truth, parsed)
        print(
            f"  {dt:.1f}s  recall={sc['recall']}  prec={sc['precision']}  "
            f"amt_exact={sc['amount_exact_of_matched']}  "
            f"date_exact={sc['date_exact_of_matched']}  "
            f"(truth={sc['n_truth']} ext={sc['n_extracted']} match={sc['n_matched']})",
            flush=True,
        )
        results[docid] = {
            "doc_type_truth": truth.get("doc_type"),
            "doc_type_extracted": (parsed or {}).get("doc_type"),
            "latency_s": round(dt, 2),
            "usage": usage,
            "raw_response": raw,
            "parsed_ok": parsed is not None,
            "score": sc,
            "truth": truth,
        }

    out = os.path.join(OUTDIR, "vlm_bakeoff_results.json")
    with open(out, "w") as fh:
        json.dump(results, fh, indent=2, default=str)
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
