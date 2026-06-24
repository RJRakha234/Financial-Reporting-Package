"""Render the casting result as a self-contained, human-readable HTML page.

The page shows, for every line item, exactly how the figure was cast — the
year-to-date (six-month) figure set against the current quarter plus the prior
quarter — so a reviewer can see the working, not just a pass/fail.
"""

from __future__ import annotations

import html
from collections import OrderedDict

from .casting import CastResult
from .numbers import format_number

_STATUS = {
    "ok": ("OK", "ok"),
    "mismatch": ("DOES NOT CAST", "bad"),
    "unverified": ("review", "warn"),
    "not_additive": ("not cast (per-share)", "muted"),
}

_CSS = """
:root{--green:#1e7e34;--greenbg:#e6f4ea;--red:#c0392b;--redbg:#fdecea;
--amber:#9c6500;--amberbg:#fff6e0;--muted:#777;--line:#e3e3e3;--ink:#222;--head:#1f4e78}
*{box-sizing:border-box}
body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
color:var(--ink);margin:0;background:#f7f8fa}
.wrap{max-width:1180px;margin:0 auto;padding:28px 22px 60px}
h1{font-size:24px;margin:0 0 4px}
.sub{color:var(--muted);font-size:13px;margin-bottom:18px;word-break:break-all}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin:14px 0 26px}
.card{border:1px solid var(--line);border-radius:10px;padding:12px 16px;background:#fff;min-width:120px}
.card .n{font-size:26px;font-weight:700}
.card .l{font-size:12px;color:var(--muted)}
.card.ok .n{color:var(--green)} .card.bad .n{color:var(--red)} .card.warn .n{color:var(--amber)}
.banner{padding:12px 16px;border-radius:10px;font-weight:600;margin-bottom:22px}
.banner.ok{background:var(--greenbg);color:var(--green)}
.banner.bad{background:var(--redbg);color:var(--red)}
h2{font-size:16px;margin:26px 0 8px;color:var(--head);border-bottom:2px solid var(--head);padding-bottom:4px}
table{border-collapse:collapse;width:100%;background:#fff;font-size:13px;
border:1px solid var(--line);border-radius:8px;overflow:hidden}
th,td{padding:7px 10px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
th{background:#f0f3f7;color:#333;font-weight:600;text-align:right;position:sticky;top:0}
th.l,td.l{text-align:left}
td.num{font-variant-numeric:tabular-nums}
td.op{color:var(--muted);text-align:center;width:18px}
tr.ok td.res{color:var(--green);font-weight:600}
tr.bad{background:var(--redbg)} tr.bad td.res{color:var(--red);font-weight:700}
tr.warn{background:var(--amberbg)} tr.muted td{color:var(--muted)}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;font-weight:700}
.pill.ok{background:var(--greenbg);color:var(--green)}
.pill.bad{background:var(--redbg);color:var(--red)}
.pill.warn{background:var(--amberbg);color:var(--amber)}
.pill.muted{background:#eee;color:var(--muted)}
.diff0{color:var(--muted)} .diffx{color:var(--red);font-weight:700}
td.basis{color:var(--muted);font-size:12px}
footer{color:var(--muted);font-size:12px;margin-top:30px}
.legend{font-size:12px;color:var(--muted);margin:8px 0 0}
"""


def _num(v):
    return "" if v is None else format_number(v)


def _row_html(c: CastResult, check) -> str:
    status = check.status(c.tolerance)
    _, cls = _STATUS[status]
    diff = check.difference
    diff_cls = "diff0" if (diff in (0, None) or status == "not_additive") else "diffx"
    pill_label, pill_cls = _STATUS[status]
    return (
        f'<tr class="{cls}">'
        f'<td class="l">{html.escape(check.label.strip())}</td>'
        f'<td>{check.year}</td>'
        f'<td class="num res">{_num(check.six_month)}</td>'
        f'<td class="op">=</td>'
        f'<td class="num">{_num(check.current_quarter)}</td>'
        f'<td class="op">+</td>'
        f'<td class="num">{_num(check.prior_quarter)}</td>'
        f'<td class="op">=</td>'
        f'<td class="num">{_num(check.expected)}</td>'
        f'<td class="num {diff_cls}">{_num(diff)}</td>'
        f'<td class="l basis">{html.escape(check.basis)}</td>'
        f'<td><span class="pill {pill_cls}">{html.escape(pill_label)}</span></td>'
        f'</tr>'
    )


def _section(result: CastResult, title: str, checks: list) -> str:
    head = (
        '<tr><th class="l">Line item</th><th>Year</th>'
        '<th>Year-to-date<br>(6M)</th><th class="op"></th>'
        '<th>Current qtr<br>(3M)</th><th class="op"></th>'
        '<th>Prior qtr<br>(3M)</th><th class="op"></th>'
        '<th>Expected</th><th>Diff</th><th class="l">Basis</th>'
        '<th>Status</th></tr>'
    )
    rows = "".join(_row_html(result, c) for c in checks)
    return f'<h2>{html.escape(title)}</h2><table>{head}{rows}</table>'


def to_html(result: CastResult) -> str:
    tol = result.tolerance
    counts = {"ok": 0, "mismatch": 0, "unverified": 0, "not_additive": 0}
    for c in result.checks:
        counts[c.status(tol)] += 1

    # Group checks by their source table, preserving document order.
    groups: "OrderedDict[tuple, list]" = OrderedDict()
    for c in result.checks:
        key = (c.note, c.title)
        groups.setdefault(key, []).append(c)

    banner_cls = "bad" if result.mismatches else "ok"
    banner_txt = (
        f"{len(result.mismatches)} figure(s) do not cast"
        if result.mismatches else "Every additive figure casts correctly"
    )

    cards = "".join([
        f'<div class="card ok"><div class="n">{counts["ok"]}</div>'
        f'<div class="l">cast correctly</div></div>',
        f'<div class="card bad"><div class="n">{counts["mismatch"]}</div>'
        f'<div class="l">do not cast</div></div>',
        f'<div class="card warn"><div class="n">{counts["unverified"]}</div>'
        f'<div class="l">unverified</div></div>',
        f'<div class="card"><div class="n">{counts["not_additive"]}</div>'
        f'<div class="l">per-share (not cast)</div></div>',
    ])

    sections = "".join(
        _section(result, f"{note + ' · ' if note and note != '?' else ''}{title}", chks)
        for (note, title), chks in groups.items()
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Casting report</title><style>{_CSS}</style></head>
<body><div class="wrap">
<h1>Casting report</h1>
<div class="sub">Current period: {html.escape(result.current_pdf)}<br>
Prior period: {html.escape(result.prior_pdf)} &nbsp;·&nbsp; tolerance ±{format_number(tol)}</div>
<div class="banner {banner_cls}">{html.escape(banner_txt)}</div>
<div class="cards">{cards}</div>
<p class="legend">Each row shows the casting identity:
<b>year-to-date (6M) = current quarter (3M) + prior quarter (3M)</b>.
Differences within ±{format_number(tol)} (rounding) are treated as OK; the exact
difference is shown regardless. Per-share and share-count rows are listed but not
cast (they are averages, not additive).</p>
{sections}
<footer>Generated by fincheck · casting check · runs entirely offline.</footer>
</div></body></html>"""


def write_html(result: CastResult, path: str) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(to_html(result))
    return path
