"""Generate a tiny pair of interim statements for testing the casting checker.

Writes two PDFs next to this script:

* ``cast_current.pdf`` — a current-period statement with three-month and
  six-month columns for two years (period ended 30 Sep);
* ``cast_prior.pdf``  — the prior-period statement with the earlier three-month
  column (period ended 30 Jun).

The figures are arranged so every line item casts (six-month == current three
months + prior three months) **except** one deliberate error, so a checker can
be asserted against a known answer.
"""

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

# Right-edge x positions of the value columns.
_C3_CUR, _C3_PRI = 300, 360            # current-year / prior-year, three-month
_C6_CUR, _C6_PRI = 460, 520            # current-year / prior-year, six-month
_LABEL_X = 50


def _row(c, y, label, *values, right_edges):
    c.setFont("Helvetica", 9)
    c.drawString(_LABEL_X, y, label)
    for value, x in zip(values, right_edges):
        c.drawRightString(x, y, value)


def _current(path):
    c = canvas.Canvas(path, pagesize=A4)
    _, height = A4
    y = height - 70
    c.setFont("Helvetica-Bold", 11)
    c.drawString(_LABEL_X, y, "Condensed Consolidated Statement of Profit and Loss")
    y -= 22
    # Period header: the word "Six" marks where the six-month block starts.
    c.setFont("Helvetica", 9)
    c.drawString(220, y, "Three months ended September 30,")
    c.drawString(410, y, "Six months ended September 30,")
    y -= 14
    edges = [_C3_CUR, _C3_PRI, _C6_CUR, _C6_PRI]
    _row(c, y, "", "2025", "2024", "2025", "2024", right_edges=edges)
    y -= 18
    # 6M(2025) for Cost is wrong on purpose (60+62 = 122, stated 125).
    rows = [
        ("Revenue from operations", "100", "90", "210", "185"),
        ("Cost of sales", "60", "55", "125", "110"),
        ("Profit for the period", "40", "35", "85", "75"),
        ("Basic (₹)", "1.20", "1.05", "2.40", "2.10"),  # non-additive
    ]
    for label, *vals in rows:
        _row(c, y, label, *vals, right_edges=edges)
        y -= 16
    c.showPage()
    c.save()


def _prior(path):
    c = canvas.Canvas(path, pagesize=A4)
    _, height = A4
    y = height - 70
    c.setFont("Helvetica-Bold", 11)
    c.drawString(_LABEL_X, y, "Condensed Consolidated Statement of Profit and Loss")
    y -= 22
    c.setFont("Helvetica", 9)
    c.drawString(220, y, "Three months ended June 30,")
    y -= 14
    edges = [_C3_CUR, _C3_PRI]
    _row(c, y, "", "2025", "2024", right_edges=edges)
    y -= 18
    rows = [
        ("Revenue from operations", "110", "95"),
        ("Cost of sales", "62", "55"),
        ("Profit for the period", "45", "40"),
        ("Basic (₹)", "1.20", "1.05"),
    ]
    for label, *vals in rows:
        _row(c, y, label, *vals, right_edges=edges)
        y -= 16
    c.showPage()
    c.save()


def build(current="cast_current.pdf", prior="cast_prior.pdf"):
    _current(current)
    _prior(prior)
    return current, prior


if __name__ == "__main__":
    cur, pri = build()
    print(f"wrote {cur} and {pri}")
