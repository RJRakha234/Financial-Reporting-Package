#!/usr/bin/env python3
"""Generate a small sample financial-statement PDF for the demo/tests."""

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

OUT = Path(__file__).parent / "sample_statement.pdf"

# (label, value_text) — value "" for a heading row.
BALANCE_SHEET = [
    ("ASSETS", ""),
    ("Non-current assets", ""),
    ("Property, plant and equipment", "12,450"),
    ("Goodwill", "1,200"),
    ("Intangible assets", "800"),
    ("Total non-current assets", "14,450"),
    ("Current assets", ""),
    ("Inventories", "6,800"),
    ("Trade receivables", "3,400"),
    ("Cash and cash equivalents", "8,750"),
    ("Total current assets", "18,950"),
    ("Total assets", "33,400"),
]


def main():
    doc = SimpleDocTemplate(str(OUT), pagesize=A4, topMargin=2 * cm, bottomMargin=2 * cm)
    styles = getSampleStyleSheet()
    data = [[lbl, val] for lbl, val in BALANCE_SHEET]
    table = Table(data, colWidths=[11 * cm, 4 * cm])
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ]
        )
    )
    story = [
        Paragraph("Acme Manufacturing Ltd", styles["Title"]),
        Paragraph("Balance Sheet as at 31 December 2024 (INR '000)", styles["Heading2"]),
        Spacer(1, 0.4 * cm),
        table,
    ]
    doc.build(story)
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
