"""Render demo/scenarios.py into real PNG receipts (demo/receipts/rNN.png) and
a manifest of who submits which file. The images print only what a real
receipt prints; the manifest carries no expected verdicts, clauses or
categories.

Usage: python demo/generate_demo_data.py
"""

import json
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from playwright.sync_api import sync_playwright

from scenarios import EMPLOYEES, S

OUT = HERE / "receipts"
C = Decimal("0.01")


def q(x) -> Decimal:
    return Decimal(str(x)).quantize(C, rounding=ROUND_HALF_UP)


def split(total: Decimal, n: int) -> list[Decimal]:
    """n positive amounts summing exactly to total."""
    weights = [3, 2, 1, 2][:n] if n <= 4 else [1] * n
    parts = [q(total * w / sum(weights)) for w in weights]
    parts[-1] = total - sum(parts[:-1])
    return parts


STYLE = """<style>body{font-family:%s;width:380px;padding:18px;background:#fff;color:#111}
h1{font-size:17px;text-align:center;margin:0 0 4px}.c{text-align:center;font-size:12px;color:#444}
table{width:100%%;border-collapse:collapse;font-size:12px;margin-top:10px}td,th{padding:3px 0;border-bottom:1px dashed #aaa;text-align:left}
td.r,th.r{text-align:right}.tot{font-weight:bold;font-size:14px;border-top:2px solid #000}.m{font-size:12px;margin:2px 0}</style>"""
FONTS = ["Arial, sans-serif", "Georgia, serif", "'Courier New', monospace"]


def money(x, cur) -> str:
    return f"{'$' if cur == 'USD' else 'Rs. '}{x:,.2f}"


def html_for(i: int, s: dict) -> str:
    cur = s.get("currency", "INR")
    head = f"<h1>{s['vendor']}</h1><div class='c'>{s['city']}</div>"
    font = FONTS[i % len(FONTS)]
    kind = s["kind"]
    rows, meta, sub, taxes, total = "", [], None, [], None
    if kind == "restaurant":
        total = q(s["total"])
        sub = q(total / Decimal("1.05"))
        cg = q((total - sub) / 2)
        taxes = [("CGST 2.5%", cg), ("SGST 2.5%", total - sub - cg)]
        amounts = split(sub, len(s["items"]))
        rows = "".join(f"<tr><td>{n}</td><td>1</td><td class='r'>{a:,.2f}</td></tr>" for n, a in zip(s["items"], amounts))
        meta = [f"Bill date: {s['date']}"] + s.get("extra", [])
        cols = "<tr><th>Item</th><th>Qty</th><th class='r'>Amount</th></tr>"
    elif kind == "hotel":
        rate = q(s["rate"]) if s.get("rate") else None
        if rate is None:   # a single-night stay defined by its total
            total = q(s["total"])
            sub = q(total / Decimal("1.12"))
            room = sub
        else:
            room = q(rate * s["nights"])
            sub = room + sum((q(a) for _, a in s["extras"]), Decimal(0))
            total = None
        lines = [(f"Room charge x {s['nights']} night(s)", room)] + [(d, q(a)) for d, a in s["extras"]]
        gst_rate = Decimal("0.12") if cur == "INR" else Decimal("0")
        tax = q(sub * gst_rate)
        if total is None:
            total = sub + tax
        else:
            tax = total - sub
        taxes = [("GST 12%", tax)] if cur == "INR" else []
        rows = "".join(f"<tr><td>{n}</td><td>1</td><td class='r'>{a:,.2f}</td></tr>" for n, a in lines)
        from datetime import date, timedelta
        ci = date.fromisoformat(s["date"])
        meta = [f"Invoice date: {ci.isoformat()}", f"Check-in: {ci.isoformat()}",
                f"Check-out: {(ci + timedelta(days=s['nights'])).isoformat()}", f"Nights: {s['nights']}",
                f"Guest: {dict((k, n) for k, n, _, _ in EMPLOYEES)[s['emp']]}"]
        if s.get("country"):
            meta.append(f"Country: {s['country']}")
        cols = "<tr><th>Description</th><th>Qty</th><th class='r'>Amount</th></tr>"
    elif kind == "taxi":
        total = q(s["total"])
        rows = f"<tr><td>Fare - {s['route']}</td><td>1</td><td class='r'>{total:,.2f}</td></tr>"
        meta = [f"Trip date: {s['date']}", "Payment: UPI"]
        cols = "<tr><th>Description</th><th>Qty</th><th class='r'>Amount</th></tr>"
    elif kind == "telecom":
        total = q(s["total"])
        sub = q(total / Decimal("1.18"))
        taxes = [("GST 18%", total - sub)]
        rows = f"<tr><td>Monthly plan charges</td><td>1</td><td class='r'>{sub:,.2f}</td></tr>"
        meta = [f"Bill date: {s['date']}", f"Account no: {s['account']}", f"Billing period: month of {s['date'][:7]}"]
        cols = "<tr><th>Description</th><th>Qty</th><th class='r'>Amount</th></tr>"
    else:  # generic
        total = q(s["total"])
        sub = q(total / Decimal("1.18"))
        taxes = [("GST 18%", total - sub)]
        amounts = split(sub, len(s["items"]))
        rows = "".join(f"<tr><td>{n}</td><td>1</td><td class='r'>{a:,.2f}</td></tr>" for n, a in zip(s["items"], amounts))
        meta = [f"Invoice date: {s['date']}"]
        cols = "<tr><th>Item</th><th>Qty</th><th class='r'>Amount</th></tr>"
    body = "".join(f"<div class='m'>{m}</div>" for m in meta)
    tail = ""
    if sub is not None:
        tail += f"<tr><td colspan=2>Subtotal</td><td class='r'>{sub:,.2f}</td></tr>"
    for name, amt in taxes:
        tail += f"<tr><td colspan=2>{name}</td><td class='r'>{amt:,.2f}</td></tr>"
    tail += f"<tr class='tot'><td colspan=2>TOTAL</td><td class='r'>{money(total, cur)}</td></tr>"
    return f"<!doctype html><html><head><meta charset='utf-8'>{STYLE % font}</head><body>{head}{body}<table>{cols}{rows}{tail}</table></body></html>"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    items = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for i, s in enumerate(S, 1):
            name = f"r{i:02d}.png"
            page = browser.new_page()
            page.set_content(html_for(i, s))
            page.screenshot(path=str(OUT / name), full_page=True)
            page.close()
            items.append({"file": f"receipts/{name}", "employee": s["emp"], "claim": s["claim"], "edit": s.get("edit")})
        browser.close()
    manifest = {
        "note": "Who submits which file. No expected verdicts, clauses or categories -- this is a demo script, not an eval set.",
        "employees": [{"key": k, "name": n, "grade": g, "base_city": c} for k, n, g, c in EMPLOYEES],
        "items": items,
    }
    (HERE / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"rendered {len(items)} receipts -> {OUT}")


if __name__ == "__main__":
    main()
