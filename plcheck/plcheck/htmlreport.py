"""Bonus HTML error-summary report for a single P&L check.

Purely additive: it reads the same Evaluation the CLI already produces and
renders a self-contained .html file. It does not touch the check-workbook
generation. Differences smaller than ``min_amount`` (default 1) are ignored.

Four sections:
    A  PL Check Summary       - LC tie-out / FX / consolidation / net-profit
    B  Minority Interest      - breaks landing on the Minority Interest line
    C  LC-Consol Check        - tie-out to the consolidation-entry tracker
    D  Entity Reconciler      - company codes missing from one of the inputs
"""

from __future__ import annotations

import html

from . import config as C
from . import inputs

_KIND_LABEL = {"lc": "LC tie-out", "fx": "FX conversion",
               "consol": "GC consolidation", "lc_consol": "LC-Consol"}

_CSS = """
body{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#222}
h1{font-size:20px;margin:0 0 4px} .sub{color:#666;margin:0 0 18px;font-size:13px}
h2{font-size:15px;margin:26px 0 8px;border-bottom:2px solid #4472C4;padding-bottom:4px}
table{border-collapse:collapse;width:100%;font-size:13px;margin-top:6px}
th{background:#4472C4;color:#fff;text-align:left;padding:6px 8px;font-weight:600}
td{padding:5px 8px;border-bottom:1px solid #e3e3e3}
tr:nth-child(even) td{background:#f7f9fc}
.num{text-align:right;font-variant-numeric:tabular-nums}
.bad{color:#9C0006;font-weight:600}
.ok{color:#006100;font-weight:600;background:#e9f6ec;padding:8px 10px;border-radius:4px;display:inline-block}
.pill{font-size:11px;color:#666}
"""


def _fmt(x) -> str:
    try:
        return f"{float(x):,.2f}"
    except (TypeError, ValueError):
        return html.escape(str(x))


def _esc(x) -> str:
    return html.escape("" if x is None else str(x))


def _rows_table(headers, rows) -> str:
    if not rows:
        return '<p class="ok">No differences found.</p>'
    th = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    body = []
    for r in rows:
        tds = []
        for cell, numeric, bad in r:
            cls = "num" if numeric else ""
            if bad:
                cls = (cls + " bad").strip()
            tds.append(f'<td class="{cls}">{cell if numeric else _esc(cell)}</td>'
                       if not numeric else f'<td class="{cls}">{_fmt(cell)}</td>')
        body.append("<tr>" + "".join(tds) + "</tr>")
    return f"<table><thead><tr>{th}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _remark(d) -> str:
    k = d.kind
    if k == "lc":
        return f"Report LC {_fmt(d.stated)} vs source {_fmt(d.expected)}"
    if k == "fx":
        return f"GC-Balance {_fmt(d.stated)} vs (LC+Consol)×rate {_fmt(d.expected)}"
    if k == "consol":
        return f"GC-Total {_fmt(d.stated)} vs sum of GC blocks {_fmt(d.expected)}"
    if k == "lc_consol":
        return f"Report {_fmt(d.stated)} vs tracker Dr−Cr {_fmt(d.expected)}"
    return ""


def build_html(report, ev, *, title, tb_codes, agg_codes, is_minority,
               min_amount: float = 1.0) -> str:
    # only real GL lines with a group account number and a difference >= min
    big = [d for d in ev.diffs
           if abs(d.delta) >= min_amount and d.account is not None]

    def acct(d):
        return d.account

    # --- A: PL check (lc / fx / consol), excluding the Minority line ---------
    a_rows = []
    for d in big:
        if d.kind == "lc_consol" or is_minority(d.category):
            continue
        a_rows.append([
            (d.entity, False, False), (acct(d), False, False),
            (d.category, False, False), (_KIND_LABEL.get(d.kind, d.kind), False, False),
            (d.stated, True, False), (d.expected, True, False),
            (d.delta, True, True), (_remark(d), False, False)])
    for n in ev.net_profit_flagged():
        if abs(n.tie_check) < min_amount and abs(n.calc_check) < min_amount:
            continue
        a_rows.append([
            (n.entity, False, False), ("(net profit)", False, False),
            ("Net Profit reconciliation", False, False), ("Net profit", False, False),
            (n.net_profit, True, False), (-n.tb_net_profit, True, False),
            (n.tie_check, True, True),
            (f"calc-check {_fmt(n.calc_check)}, TB tie {_fmt(n.tie_check)}", False, False)])
    a_html = _rows_table(
        ["Entity", "Group Account", "Section", "Check", "Stated", "Expected",
         "Difference", "Remark"], a_rows)

    # --- B: Minority Interest line -------------------------------------------
    b_rows = [[
        (d.entity, False, False), (acct(d), False, False),
        (_KIND_LABEL.get(d.kind, d.kind), False, False),
        (d.stated, True, False), (d.expected, True, False),
        (d.delta, True, True), (_remark(d), False, False)]
        for d in big if d.kind != "lc_consol" and is_minority(d.category)]
    b_html = _rows_table(
        ["Entity", "Group Account", "Check", "Stated", "Expected",
         "Difference", "Remark"], b_rows)

    # --- C: LC-Consol tie-out ------------------------------------------------
    c_rows = [[
        (d.entity, False, False), (acct(d), False, False),
        (d.category, False, False),
        (d.stated, True, False), (d.expected, True, False),
        (d.delta, True, True), (_remark(d), False, False)]
        for d in big if d.kind == "lc_consol"]
    c_html = _rows_table(
        ["Entity", "Group Account", "Section", "Report value", "Tracker (Dr−Cr)",
         "Difference", "Remark"], c_rows)

    # --- D: Entity reconciler ------------------------------------------------
    srcs = [("PL Report", {c.upper() for c in
                           (e.code for e in report.entities)
                           if inputs.looks_like_company_code(c)}),
            ("Real Time TB", {c.upper() for c in tb_codes})]
    if agg_codes:
        srcs.append(("Aggregate Exp", {c.upper() for c in agg_codes}))
    union = []
    seen = set()
    for _n, codes in srcs:
        for c in sorted(codes):
            if c not in seen:
                seen.add(c)
                union.append(c)
    d_rows = []
    for code in union:
        missing = [n for n, codes in srcs if code not in codes]
        if missing:
            cells = [(code, False, False)]
            cells += [("Yes" if code in codes else "-", False, code not in codes)
                      for _n, codes in srcs]
            cells.append(("missing from: " + ", ".join(missing), False, True))
            d_rows.append(cells)
    d_head = ["Company Code"] + [n for n, _c in srcs] + ["Note"]
    d_html = (_rows_table(d_head, d_rows) if d_rows
              else '<p class="ok">All company codes present in every source.</p>')

    total = len(a_rows) + len(b_rows) + len(c_rows) + len(d_rows)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{_esc(title)} - check summary</title><style>{_CSS}</style></head><body>
<h1>{_esc(title)} — Check Error Summary</h1>
<p class="sub">Differences below {min_amount:g} are ignored.
&nbsp;<span class="pill">{total} item(s) reported</span></p>
<h2>A. PL Check Summary</h2>{a_html}
<h2>B. Minority Interest Check</h2>{b_html}
<h2>C. LC-Consol Check</h2>{c_html}
<h2>D. Entity Reconciler</h2>{d_html}
</body></html>"""
