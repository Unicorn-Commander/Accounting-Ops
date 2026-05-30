#!/usr/bin/env python3
"""Probe the current SC1120 PDF layout for coordinate refresh.

Run this whenever SCDOR republishes the SC1120 PDF. The script extracts the
position of every line label and key header field, then prints suggested
updates to ``app/exports/sc_form_prefill.py::SC1120_PAGE1_LINE_COORDS`` and
``SC1120_PAGE1_HEADER_COORDS``.

Usage:
    bash scripts/download_scdor_forms.sh           # refresh SC PDFs first
    python scripts/probe_sc1120_layout.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
SC1120 = ROOT / "data" / "scdor_forms" / "SC1120.pdf"


def main() -> int:
    if not SC1120.exists():
        print(f"ERROR: {SC1120} not present. "
              "Run scripts/download_scdor_forms.sh first.", file=sys.stderr)
        return 1

    try:
        from pypdf import PdfReader
    except ImportError:
        print("ERROR: pypdf not installed. pip install pypdf", file=sys.stderr)
        return 1

    r = PdfReader(SC1120)
    p = r.pages[0]

    texts: list[tuple[float, float, str]] = []

    def visit_text(text, cm, tm, fontDict, fontSize):
        x, y = tm[4], tm[5]
        if text.strip():
            texts.append((y, x, text.strip()))

    p.extract_text(visitor_text=visit_text)
    texts.sort(key=lambda t: (-t[0], t[1]))

    print("# Header field coordinates")
    print("# (look for the line BELOW each label — that's where you draw text)")
    for y, x, t in texts:
        lower = t.lower()
        if x < 250 and any(k in lower for k in ("fein", "name", "mailing address",
                                                 "income tax period",
                                                 "license fee period",
                                                 "city")):
            print(f'    # label "{t[:40]}" at y={y:.1f} → draw text at y={y + 2:.1f}')

    print()
    print("# Line-item dollar field y-coordinates")
    print("# (x ≈ 530 right-aligned for whole-dollar column)")
    seen = set()
    for y, x, t in texts:
        m = re.match(r"^\s*(\d+)[.a-g]?\s", t)
        if m and x < 100:
            line = m.group(1)
            if line not in seen:
                seen.add(line)
                print(f'    "{line}": (530.0, {y:.1f}),')

    return 0


if __name__ == "__main__":
    sys.exit(main())
