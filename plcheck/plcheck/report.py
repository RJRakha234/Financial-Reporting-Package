"""Human- and machine-readable summaries of an :class:`Evaluation`."""

from __future__ import annotations

import json

from .evaluate import Evaluation

_KIND = {"lc": "LC tie-out", "fx": "FX conversion", "consol": "GC consolidation"}


def _fmt(n: float) -> str:
    return f"{n:,.2f}"


def to_console(ev: Evaluation) -> str:
    lines: list[str] = []
    flagged = ev.flagged()
    np_flagged = ev.net_profit_flagged()

    if ev.missing_blocks:
        lines.append("⚠ Expected column blocks not found in the report (their "
                     "checks were skipped): " + ", ".join(ev.missing_blocks))
        lines.append("  Check the block headings in row 2 match the expected "
                     "names, e.g. 'GC - Total'.")
        lines.append("")

    if ev.unmapped_categories:
        lines.append("⚠ Unmapped P&L categories (no rule, LC tie-out skipped): "
                     + ", ".join(ev.unmapped_categories))
        lines.append("")

    if not flagged and not np_flagged:
        lines.append("✓ All reconciliations tie out "
                     f"(tolerance ±{ev.tolerance:g}).")
        return "\n".join(lines)

    if flagged:
        lines.append(f"✗ {len(flagged)} difference(s) exceed ±{ev.tolerance:g}:\n")
        for d in flagged:
            acct = d.account if d.account not in (None, "") else "(subtotal)"
            lines.append(
                f"  · {_KIND[d.kind]:<16} {d.category} / {acct} "
                f"{d.description}".rstrip())
            lines.append(
                f"      {d.entity}: stated {_fmt(d.stated)}  "
                f"expected {_fmt(d.expected)}  (off by {_fmt(d.delta)})")

    if np_flagged:
        lines.append("\n✗ Net-profit reconciliation off for: " +
                     ", ".join(n.entity for n in np_flagged))
        for n in np_flagged:
            lines.append(
                f"      {n.entity}: calc-check {_fmt(n.calc_check)}  "
                f"vs-TB {_fmt(n.tie_check)}")
    return "\n".join(lines)


def to_dict(ev: Evaluation) -> dict:
    return {
        "ok": ev.ok,
        "tolerance": ev.tolerance,
        "unmapped_categories": ev.unmapped_categories,
        "missing_blocks": ev.missing_blocks,
        "differences": [
            {
                "kind": d.kind, "category": d.category, "account": d.account,
                "description": d.description, "entity": d.entity,
                "stated": d.stated, "expected": d.expected, "delta": d.delta,
            }
            for d in ev.flagged()
        ],
        "net_profit": [
            {
                "entity": n.entity, "income": n.income, "expense": n.expense,
                "net_profit": n.net_profit, "tb_net_profit": n.tb_net_profit,
                "calc_check": n.calc_check, "tie_check": n.tie_check,
            }
            for n in ev.net_profit
        ],
    }


def to_json(ev: Evaluation) -> str:
    return json.dumps(to_dict(ev), indent=2, default=str)
