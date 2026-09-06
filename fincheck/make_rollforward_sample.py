#!/usr/bin/env python3
"""Generate two sample filings to demonstrate the rollforward checker.

* ``rf_current.pdf`` — results for the **quarter ended 30 June 2026**, with
  comparative columns for the quarter ended 30 June 2025 and the year ended
  31 March 2026.
* ``rf_prior.pdf`` — the previously published results for the **quarter ended
  30 June 2025**.

The overlapping period between the two filings is the *quarter ended
30 June 2025*. In the current filing that comparative column was rolled forward
with a deliberate error: **Other income** is shown as 120, but it was originally
published as 95. That mis-keyed figure also flows through Total income and the
profit lines, so the checker flags the whole chain — exactly what a real
restatement/rollforward error looks like.
"""

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

HERE = Path(__file__).parent


def money(v):
    return "" if v is None else f"{v:,}"


def statement(period_headers, rows):
    """period_headers: list of (type_phrase, date) for each numeric column.
    rows: list of (label, [values...], is_total)."""
    n = len(period_headers)
    data = [
        [""] + [h[0] for h in period_headers],
        ["Particulars"] + [h[1] for h in period_headers],
    ]
    total_row_idx = []
    for label, values, is_total in rows:
        data.append([label] + [money(v) for v in values])
        if is_total:
            total_row_idx.append(len(data) - 1)

    table = Table(data, colWidths=[7 * cm] + [3 * cm] * n)
    style = TableStyle(
        [
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("FONTNAME", (1, 0), (-1, 1), "Helvetica-Bold"),
            ("LINEBELOW", (0, 1), (-1, 1), 0.5, colors.black),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
    )
    for i in total_row_idx:
        style.add("LINEABOVE", (1, i), (-1, i), 0.5, colors.black)
        style.add("FONTNAME", (0, i), (-1, i), "Helvetica-Bold")
    table.setStyle(style)
    return table


def build(path, title, period_headers, rows):
    doc = SimpleDocTemplate(
        str(path), pagesize=A4,
        leftMargin=1.5 * cm, rightMargin=1.5 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
    )
    styles = getSampleStyleSheet()
    story = [
        Paragraph("Acme Industries Ltd", styles["Title"]),
        Paragraph(title, styles["Heading2"]),
        Paragraph("(INR in lakhs)", styles["Normal"]),
        Spacer(1, 0.4 * cm),
        statement(period_headers, rows),
    ]
    doc.build(story)
    print(f"Wrote {path}")


def main():
    # ---- Current filing: quarter ended 30 June 2026 -----------------------
    # columns: Q ended 30.06.2026 | Q ended 30.06.2025 (comparative) |
    #          Year ended 31.03.2026 (comparative)
    current_rows = [
        ("Revenue from operations", [5200, 4800, 19500], False),
        ("Other income",           [150,  120,  600],    False),  # 120 is WRONG
        ("Total income",           [5350, 4920, 20100],  True),
        ("Cost of materials consumed", [3100, 2900, 11800], False),
        ("Employee benefit expense",   [700,  650,  2700],  False),
        ("Finance costs",              [90,   80,   350],   False),
        ("Depreciation and amortisation", [210, 200, 820],  False),
        ("Other expenses",             [450,  400,  1700],  False),
        ("Total expenses",         [4550, 4230, 17370],  True),
        ("Profit before tax",      [800,  690,  2730],   False),
        ("Tax expense",            [210,  180,  700],    False),
        ("Profit for the period",  [590,  510,  2030],   False),
    ]
    build(
        HERE / "rf_current.pdf",
        "Statement of Financial Results for the quarter ended 30 June 2026",
        [("Quarter ended", "30.06.2026"),
         ("Quarter ended", "30.06.2025"),
         ("Year ended", "31.03.2026")],
        current_rows,
    )

    # ---- Prior filing: quarter ended 30 June 2025 (as published) ----------
    # Its current column (30.06.2025) is the source of truth for the
    # comparative above. Other income was published as 95, not 120.
    prior_rows = [
        ("Revenue from operations", [4800, 4500, 18200], False),
        ("Other income",           [95,   110,  540],    False),  # published 95
        ("Total income",           [4895, 4610, 18740],  True),
        ("Cost of materials consumed", [2900, 2750, 11000], False),
        ("Employee benefit expense",   [650,  600,  2500],  False),
        ("Finance costs",              [80,   75,   320],   False),
        ("Depreciation and amortisation", [200, 190, 780],  False),
        ("Other expenses",             [400,  380,  1600],  False),
        ("Total expenses",         [4230, 3995, 16200],  True),
        ("Profit before tax",      [665,  615,  2540],   False),
        ("Tax expense",            [180,  165,  660],    False),
        ("Profit for the period",  [485,  450,  1880],   False),
    ]
    build(
        HERE / "rf_prior.pdf",
        "Statement of Financial Results for the quarter ended 30 June 2025",
        [("Quarter ended", "30.06.2025"),
         ("Quarter ended", "30.06.2024"),
         ("Year ended", "31.03.2025")],
        prior_rows,
    )


if __name__ == "__main__":
    main()
