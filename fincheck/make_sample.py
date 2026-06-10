#!/usr/bin/env python3
"""Generate a sample financial-statement PDF with deliberate footing errors.

Two additive totals are intentionally wrong so the checker has something to
catch:
  * "Total current assets" is overstated by 100.
  * "Total expenses" is understated by 250.
Everything else foots correctly, including the nested grand totals. "Profit for
the year" is a derived figure (income minus expenses), not an additive subtotal,
so the checker correctly leaves it alone.
"""

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.lib.styles import getSampleStyleSheet

OUT = Path(__file__).parent / "sample_financials.pdf"


def money(v):
    return f"{v:,}"


def statement_table(rows):
    """rows: list of (indent_level, label, value_or_None, is_total)."""
    data = []
    styles = []
    for i, (indent, label, value, is_total) in enumerate(rows):
        text = ("    " * indent) + label
        data.append([text, money(value) if value is not None else ""])
        if is_total:
            styles.append(("LINEABOVE", (1, i), (1, i), 0.5, colors.black))
            styles.append(("FONTNAME", (0, i), (-1, i), "Helvetica-Bold"))
    table = Table(data, colWidths=[11 * cm, 4 * cm])
    base = TableStyle(
        [
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
    )
    for s in styles:
        base.add(*s)
    table.setStyle(base)
    return table


def main():
    doc = SimpleDocTemplate(
        str(OUT),
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
    )
    styles = getSampleStyleSheet()
    story = [
        Paragraph("Acme Manufacturing Ltd", styles["Title"]),
        Paragraph("Balance Sheet as at 31 December 2024 (INR '000)", styles["Heading2"]),
        Spacer(1, 0.4 * cm),
    ]

    # indent, label, value, is_total
    balance_sheet = [
        (0, "ASSETS", None, False),
        (1, "Non-current assets", None, False),
        (2, "Property, plant and equipment", 12_450, False),
        (2, "Goodwill", 1_200, False),
        (2, "Intangible assets", 800, False),
        (1, "Total non-current assets", 14_450, True),  # 12450+1200+800 = 14450 OK
        (1, "Current assets", None, False),
        (2, "Inventories", 6_800, False),
        (2, "Trade receivables", 3_400, False),
        (2, "Cash and cash equivalents", 8_750, False),
        (1, "Total current assets", 19_050, True),  # 6800+3400+8750 = 18950 -> WRONG (+100)
        (0, "Total assets", 33_400, True),  # 14450 + 18950 = 33400 (true sum) OK
    ]
    story.append(statement_table(balance_sheet))
    story.append(Spacer(1, 0.8 * cm))

    story.append(
        Paragraph("Statement of Profit and Loss (INR '000)", styles["Heading2"])
    )
    story.append(Spacer(1, 0.4 * cm))
    income = [
        (0, "Revenue from operations", 24_800, False),
        (0, "Other income", 450, False),
        (0, "Total income", 25_250, True),  # 24800+450 = 25250 OK
        (0, "Expenses", None, False),
        (1, "Cost of materials consumed", 11_200, False),
        (1, "Employee benefit expense", 2_150, False),
        (1, "Finance costs", 600, False),
        (1, "Depreciation and amortisation", 1_300, False),
        (1, "Other expenses", 3_000, False),
        (0, "Total expenses", 18_000, True),  # 11200+2150+600+1300+3000 = 18250 -> WRONG (-250)
        (0, "Profit for the year", 7_000, False),  # derived (income - expenses), not checked
    ]
    story.append(statement_table(income))

    doc.build(story)
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
