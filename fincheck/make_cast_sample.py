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


def _row(c, y, label, *values, right_edges, indent=0):
    c.setFont("Helvetica", 9)
    c.drawString(_LABEL_X + indent, y, label)
    for value, x in zip(values, right_edges):
        c.drawRightString(x, y, value)


def _heading(c, y, label, indent=0):
    c.setFont("Helvetica-Bold", 9)
    c.drawString(_LABEL_X + indent, y, label)


# Segment matrix: three columns drawn centred so the casting extractor (which
# clusters figures by horizontal centre) sees clean columns. Every figure casts.
_SEG_CENTRES = [294, 394, 494]
_SEG_NAMES = ["Segment A", "Segment B", "Total"]


def _segment(c, y, header, rows):
    c.setFont("Helvetica", 9)
    c.drawString(_LABEL_X, y, header)
    y -= 14
    for name, x in zip(_SEG_NAMES, _SEG_CENTRES):
        c.drawCentredString(x, y, name)
    y -= 16
    for label, cy, py in rows:
        c.drawString(_LABEL_X, y, label)
        for v, x in zip(cy, _SEG_CENTRES):
            c.drawCentredString(x, y, v)
        y -= 12
        for v, x in zip(py, _SEG_CENTRES):           # comparative year, unlabelled
            c.drawCentredString(x, y, v)
        y -= 16
    return y


# Movement schedule (ROU): three asset-category columns; balance rows carry an
# inline date that lands left of the value columns. Every figure casts.
_SCH_CENTRES = [320, 410, 500]
_SCH_NAMES = ["Land", "Buildings", "Total"]


def _schedule(c, y, header, rows):
    c.setFont("Helvetica", 9)
    c.drawString(_LABEL_X, y, "Changes in the carrying value of right-of-use assets")
    y -= 12
    c.drawString(_LABEL_X, y, header)
    y -= 14
    for name, x in zip(_SCH_NAMES, _SCH_CENTRES):
        c.drawCentredString(x, y, name)
    y -= 16
    for label, vals in rows:
        c.drawString(_LABEL_X, y, label)
        for v, x in zip(vals, _SCH_CENTRES):
            c.drawRightString(x, y, v)
        y -= 14
    return y


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
    # A grouped table: the same line item ("Member") appears under two plans, so
    # it must be told apart by its sub-group heading to cast correctly.
    y -= 14
    c.setFont("Helvetica", 9)
    c.drawString(_LABEL_X, y, "Summary of grants")
    y -= 12
    c.drawString(220, y, "Three months ended September 30,")
    c.drawString(410, y, "Six months ended September 30,")
    y -= 14
    _row(c, y, "", "2025", "2024", "2025", "2024", right_edges=edges)
    y -= 16
    _heading(c, y, "Plan A"); y -= 14
    _row(c, y, "Member", "10", "8", "25", "20", right_edges=edges, indent=20); y -= 14
    _heading(c, y, "Plan B"); y -= 14
    _row(c, y, "Member", "5", "4", "12", "10", right_edges=edges, indent=20); y -= 18
    # Two segment matrices (three- and six-month) on the same page.
    y -= 20
    y = _segment(
        c, y, "Three months ended September 30, 2025 and September 30, 2024:",
        [("Revenue from operations", ["30", "20", "50"], ["28", "18", "46"]),
         ("Segment operating income", ["10", "5", "15"], ["9", "4", "13"])],
    )
    y -= 14
    _segment(
        c, y, "Six months ended September 30, 2025 and September 30, 2024:",
        [("Revenue from operations", ["65", "45", "110"], ["60", "38", "98"]),
         ("Segment operating income", ["22", "12", "34"], ["20", "9", "29"])],
    )
    # Movement schedules on a fresh page (three- and six-month).
    c.showPage()
    y = height - 70
    y = _schedule(
        c, y, "for the three months ended September 30, 2025:",
        [("Balance as at July 1, 2025", ["100", "200", "300"]),
         ("Additions", ["10", "20", "30"]),
         ("Depreciation", ["(5)", "(10)", "(15)"]),
         ("Balance as at September 30, 2025", ["105", "210", "315"])],
    )
    y -= 24
    _schedule(
        c, y, "for the six months ended September 30, 2025:",
        [("Balance as at April 1, 2025", ["90", "180", "270"]),
         ("Additions", ["25", "50", "75"]),
         ("Depreciation", ["(10)", "(20)", "(30)"]),
         ("Balance as at September 30, 2025", ["105", "210", "315"])],
    )
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
    # Grouped table (prior three-month figures); same "Member" under two plans.
    y -= 14
    c.setFont("Helvetica", 9)
    c.drawString(_LABEL_X, y, "Summary of grants")
    y -= 12
    c.drawString(220, y, "Three months ended June 30,")
    y -= 14
    _row(c, y, "", "2025", "2024", right_edges=edges)
    y -= 16
    _heading(c, y, "Plan A"); y -= 14
    _row(c, y, "Member", "15", "12", right_edges=edges, indent=20); y -= 14
    _heading(c, y, "Plan B"); y -= 14
    _row(c, y, "Member", "7", "6", right_edges=edges, indent=20); y -= 18
    y -= 20
    _segment(
        c, y, "Three months ended June 30, 2025 and June 30, 2024:",
        [("Revenue from operations", ["35", "25", "60"], ["32", "20", "52"]),
         ("Segment operating income", ["12", "7", "19"], ["11", "5", "16"])],
    )
    c.showPage()
    y = height - 70
    _schedule(
        c, y, "for the three months ended June 30, 2025:",
        [("Balance as at April 1, 2025", ["90", "180", "270"]),
         ("Additions", ["15", "30", "45"]),
         ("Depreciation", ["(5)", "(10)", "(15)"]),
         ("Balance as at June 30, 2025", ["100", "200", "300"])],
    )
    c.showPage()
    c.save()


def build(current="cast_current.pdf", prior="cast_prior.pdf"):
    _current(current)
    _prior(prior)
    return current, prior


if __name__ == "__main__":
    cur, pri = build()
    print(f"wrote {cur} and {pri}")
