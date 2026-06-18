#!/usr/bin/env python3
"""Generate two versions of the same IFRS balance sheet -- one in INR (crores)
and one in USD (millions) -- to demonstrate finmatch.

The wording is identical between the two EXCEPT for two deliberate differences,
so the tool has something to flag:
  * "Trade receivables" (INR) vs "Trade and other receivables" (USD)
  * "Goodwill" exists only in the INR version.
Everything else should come back green (matched language), and all the figures
differ (different currency) yet must NOT cause a mismatch.
"""

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

HERE = Path(__file__).parent


def _doc(path, title, unit, rows):
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(str(path), pagesize=A4)
    story = [
        Paragraph("ACME Holdings Limited", styles["Title"]),
        Paragraph(title, styles["Heading2"]),
        Paragraph(f"(Amounts in {unit})", styles["Normal"]),
        Spacer(1, 16),
    ]
    table = Table(rows, colWidths=[260, 110, 110])
    table.setStyle(
        TableStyle(
            [
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.black),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(table)
    doc.build(story)


def main():
    inr_rows = [
        ["Assets", "31 Mar 2024", "31 Mar 2023"],
        ["Property, plant and equipment", "12,450", "11,980"],
        ["Goodwill", "3,200", "3,200"],
        ["Intangible assets", "1,870", "1,640"],
        ["Trade receivables", "6,75,000", "7,20,000"],
        ["Cash and cash equivalents", "2,40,500", "1,98,300"],
        ["Total assets", "9,33,990", "9,35,120"],
    ]
    usd_rows = [
        ["Assets", "31 Mar 2024", "31 Mar 2023"],
        ["Property, plant and equipment", "149.4", "143.8"],
        ["Intangible assets", "22.4", "19.7"],
        ["Trade and other receivables", "810.0", "864.0"],
        ["Cash and cash equivalents", "288.6", "238.0"],
        ["Total assets", "1,270.8", "1,265.5"],
    ]
    _doc(HERE / "balance_sheet_inr.pdf", "Consolidated Balance Sheet", "INR crores", inr_rows)
    _doc(HERE / "balance_sheet_usd.pdf", "Consolidated Balance Sheet", "USD millions", usd_rows)
    print("wrote balance_sheet_inr.pdf and balance_sheet_usd.pdf")


if __name__ == "__main__":
    main()
