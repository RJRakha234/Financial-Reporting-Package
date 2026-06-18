"""Console, JSON and side-by-side HTML reports for a comparison Result."""

import html
import json

from .compare import Result

GREEN = "\033[32m"
RED = "\033[31m"
BOLD = "\033[1m"
RESET = "\033[0m"


def to_console(result: Result, path_a: str, path_b: str, color: bool = True) -> str:
    def c(code, text):
        return f"{code}{text}{RESET}" if color else text

    out = []
    pct = result.coverage * 100
    head = (
        f"{result.matched} lines matched · "
        f"{result.mismatched_a + result.mismatched_b} not matched · "
        f"{pct:.1f}% language match"
    )
    if result.consistent:
        out.append(c(GREEN + BOLD, "✓ Language matches across both files."))
        out.append(head)
        return "\n".join(out)

    out.append(c(RED + BOLD, "✗ Language differences found."))
    out.append(head)
    out.append("")
    n = 0
    for row in result.rows:
        if row.kind != "mismatch":
            continue
        n += 1
        a_txt = f"p{row.a.page + 1}: {row.a.text}" if row.a else "—"
        b_txt = f"p{row.b.page + 1}: {row.b.text}" if row.b else "—"
        out.append(f"{n:>3}. {c(BOLD, 'A')}  {c(RED, a_txt)}")
        out.append(f"     {c(BOLD, 'B')}  {c(RED, b_txt)}")
    return "\n".join(out)


def to_json(result: Result, path_a: str, path_b: str) -> str:
    mismatches = []
    for row in result.rows:
        if row.kind != "mismatch":
            continue
        mismatches.append(
            {
                "a": None if row.a is None else {"page": row.a.page + 1, "text": row.a.text},
                "b": None if row.b is None else {"page": row.b.page + 1, "text": row.b.text},
            }
        )
    return json.dumps(
        {
            "file_a": path_a,
            "file_b": path_b,
            "consistent": result.consistent,
            "matched": result.matched,
            "mismatched_a": result.mismatched_a,
            "mismatched_b": result.mismatched_b,
            "coverage": round(result.coverage, 4),
            "mismatches": mismatches,
        },
        indent=2,
        ensure_ascii=False,
    )


_HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>finmatch — {a} vs {b}</title>
<style>
 body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 24px; }}
 h1 {{ font-size: 18px; }} .stats {{ color:#555; margin-bottom:16px; }}
 table {{ border-collapse: collapse; width: 100%; }}
 td, th {{ border: 1px solid #ddd; padding: 6px 9px; vertical-align: top;
          font-size: 13px; white-space: pre-wrap; }}
 th {{ background:#f4f4f4; text-align:left; }}
 tr.match td {{ background:#e7f7e7; }}
 tr.mismatch td {{ background:#fde3e3; }}
 .col {{ color:#888; width:38px; text-align:center; }}
</style></head><body>
<h1>finmatch — language comparison</h1>
<div class="stats">{stats}</div>
<table><tr><th class="col"></th><th>{a}</th><th>{b}</th></tr>
{rows}
</table></body></html>
"""


def to_html(result: Result, path_a: str, path_b: str) -> str:
    rows_html = []
    for row in result.rows:
        a = "" if row.a is None else f"p{row.a.page + 1}&nbsp;&nbsp;{html.escape(row.a.text)}"
        b = "" if row.b is None else f"p{row.b.page + 1}&nbsp;&nbsp;{html.escape(row.b.text)}"
        mark = "=" if row.kind == "match" else "≠"
        rows_html.append(
            f'<tr class="{row.kind}"><td class="col">{mark}</td>'
            f"<td>{a}</td><td>{b}</td></tr>"
        )
    pct = result.coverage * 100
    stats = (
        f"{result.matched} matched · "
        f"{result.mismatched_a + result.mismatched_b} not matched · "
        f"{pct:.1f}% language match · "
        f"{'CONSISTENT' if result.consistent else 'DIFFERENCES FOUND'}"
    )
    return _HTML_TEMPLATE.format(
        a=html.escape(path_a),
        b=html.escape(path_b),
        stats=html.escape(stats),
        rows="\n".join(rows_html),
    )
