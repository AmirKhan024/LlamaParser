"""Renders a synth_content.SyntheticDoc into one of a few varied-looking
HTML documents (receipt / invoice / ticket / utility / fee), so the
rendered PNGs (via Playwright, see generate_synthetic_eval_images.py)
don't all look like the same template with different words.
"""

import random

from synth_content import CURRENCIES, SyntheticDoc

_FONTS = ["Georgia, serif", "'Courier New', monospace", "Arial, sans-serif", "'Trebuchet MS', sans-serif"]


def _rows_html(doc: SyntheticDoc) -> str:
    rows = "".join(
        f"<tr><td>{desc}</td><td>{qty}</td><td>{rate}</td><td>{amount}</td></tr>"
        for desc, qty, rate, amount in doc.line_items
    )
    return rows


def _symbol(doc: SyntheticDoc) -> str:
    return CURRENCIES.get(doc.currency, "")


def render_receipt(doc: SyntheticDoc) -> str:
    font = random.choice(_FONTS)
    lines_html = "".join(f"<div>{l}</div>" for l in doc.lines)
    rotation = random.uniform(-0.4, 0.4)
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
body {{ font-family: {font}; width: 320px; padding: 16px; background: #fff; transform: rotate({rotation}deg); }}
h1 {{ font-size: 15px; text-align: center; margin: 0 0 8px; }}
.line {{ font-size: 12px; margin-bottom: 2px; }}
table {{ width: 100%; font-size: 11px; border-collapse: collapse; margin-top: 8px; }}
td {{ padding: 2px 0; border-bottom: 1px dashed #999; }}
.total {{ font-weight: bold; font-size: 13px; margin-top: 8px; text-align: right; border-top: 1px solid #000; padding-top: 4px; }}
</style></head><body>
<h1>{doc.vendor}</h1>
{lines_html}
<table>{_rows_html(doc)}</table>
<div class="total">{doc.total_label}: {_symbol(doc)}{doc.total_amount} {doc.currency}</div>
</body></html>"""


def render_invoice(doc: SyntheticDoc) -> str:
    font = random.choice(_FONTS)
    lines_html = "".join(f"<div class='meta'>{l}</div>" for l in doc.lines)
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
body {{ font-family: {font}; width: 620px; padding: 28px; background: #fff; color: #222; }}
.header {{ border-bottom: 2px solid #333; padding-bottom: 10px; margin-bottom: 14px; }}
.header h1 {{ font-size: 20px; margin: 0; }}
.meta {{ font-size: 12px; color: #444; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 16px; font-size: 12px; }}
th, td {{ border: 1px solid #ccc; padding: 6px 8px; text-align: left; }}
th {{ background: #f2f2f2; }}
.total-row {{ font-weight: bold; font-size: 14px; text-align: right; margin-top: 12px; }}
</style></head><body>
<div class="header"><h1>{doc.vendor}</h1>{lines_html}</div>
<table><thead><tr><th>Description</th><th>Qty</th><th>Rate</th><th>Amount</th></tr></thead>
<tbody>{_rows_html(doc)}</tbody></table>
<div class="total-row">{doc.total_label}: {_symbol(doc)}{doc.total_amount} {doc.currency}</div>
</body></html>"""


def render_ticket(doc: SyntheticDoc) -> str:
    lines_html = "".join(f"<div class='row'>{l}</div>" for l in doc.lines)
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
body {{ font-family: Arial, sans-serif; width: 560px; padding: 20px; background: linear-gradient(#fefefe,#eef3fa); }}
.card {{ border: 2px solid #1a4d8f; border-radius: 8px; padding: 18px; }}
.card h1 {{ color: #1a4d8f; font-size: 18px; margin: 0 0 10px; }}
.row {{ font-size: 13px; margin-bottom: 4px; }}
table {{ width: 100%; margin-top: 10px; font-size: 12px; border-collapse: collapse; }}
td {{ padding: 3px 0; }}
.total-row {{ font-weight: bold; margin-top: 8px; text-align: right; }}
</style></head><body>
<div class="card"><h1>{doc.vendor}</h1>{lines_html}
<table>{_rows_html(doc)}</table>
<div class="total-row">{doc.total_label}: {_symbol(doc)}{doc.total_amount} {doc.currency}</div>
</div></body></html>"""


def render_utility(doc: SyntheticDoc) -> str:
    lines_html = "".join(f"<div class='meta'>{l}</div>" for l in doc.lines)
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
body {{ font-family: 'Trebuchet MS', sans-serif; width: 600px; padding: 24px; background: #fff; }}
.brand {{ color: #b8341b; font-size: 22px; font-weight: bold; margin-bottom: 6px; }}
.meta {{ font-size: 12px; color: #333; }}
table {{ width: 100%; margin-top: 14px; font-size: 12px; border-collapse: collapse; }}
td {{ padding: 5px 0; border-bottom: 1px solid #eee; }}
.total-row {{ font-size: 15px; font-weight: bold; margin-top: 10px; text-align: right; }}
</style></head><body>
<div class="brand">{doc.vendor}</div>{lines_html}
<table>{_rows_html(doc)}</table>
<div class="total-row">Amount Due: {_symbol(doc)}{doc.total_amount} {doc.currency}</div>
</body></html>"""


def render_fee(doc: SyntheticDoc) -> str:
    lines_html = "".join(f"<div class='meta'>{l}</div>" for l in doc.lines)
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
body {{ font-family: Georgia, serif; width: 480px; padding: 22px; background: #fff; border: 1px solid #ccc; }}
h1 {{ font-size: 16px; margin: 0 0 8px; }}
.meta {{ font-size: 12px; margin-bottom: 3px; }}
table {{ width: 100%; margin-top: 10px; font-size: 12px; border-collapse: collapse; }}
td {{ padding: 4px 0; }}
.total-row {{ font-weight: bold; margin-top: 8px; text-align: right; border-top: 1px solid #000; padding-top: 6px; }}
</style></head><body>
<h1>{doc.vendor}</h1>{lines_html}
<table>{_rows_html(doc)}</table>
<div class="total-row">{doc.total_label}: {_symbol(doc)}{doc.total_amount} {doc.currency}</div>
</body></html>"""


RENDERERS = {
    "receipt": render_receipt,
    "invoice": render_invoice,
    "ticket": render_ticket,
    "utility": render_utility,
    "fee": render_fee,
}


def render(doc: SyntheticDoc) -> str:
    return RENDERERS[doc.layout](doc)
