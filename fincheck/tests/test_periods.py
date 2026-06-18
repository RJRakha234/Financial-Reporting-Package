from datetime import date

from fincheck.extract import Page, Row
from fincheck.periods import detect_periods, parse_date


def test_parse_date_formats():
    assert parse_date("30 June 2025") == date(2025, 6, 30)
    assert parse_date("June 30, 2025") == date(2025, 6, 30)
    assert parse_date("30.06.2025") == date(2025, 6, 30)
    assert parse_date("31/03/2026") == date(2026, 3, 31)
    assert parse_date("31-03-2026") == date(2026, 3, 31)
    assert parse_date("2025-06-30") == date(2025, 6, 30)
    assert parse_date("31st March 2026") == date(2026, 3, 31)


def test_parse_date_rejects_non_dates():
    assert parse_date("Particulars") is None
    assert parse_date("") is None
    assert parse_date("31 Foo 2026") is None  # not a month
    assert parse_date("30.06.2025 31.03.2025") is None  # two dates, not one
    assert parse_date("31.13.2026") is None  # impossible month


def _tok(text, x0, x1, top=10):
    return {"text": text, "x0": x0, "x1": x1, "top": top, "bottom": top + 8}


def test_detect_periods_maps_dates_and_types_to_columns():
    edges = [348.0, 433.0, 518.0]
    type_row = Row(
        0, 10, 18, "Quarter ended Quarter ended Year ended", 300,
        is_header=True,
        tokens=[
            _tok("Quarter", 300, 330), _tok("ended", 331, 351),
            _tok("Quarter", 385, 415), _tok("ended", 416, 436),
            _tok("Year", 470, 500), _tok("ended", 501, 521),
        ],
    )
    date_row = Row(
        0, 28, 36, "30.06.2026 30.06.2025 31.03.2026", 320,
        tokens=[
            _tok("30.06.2026", 320, 348, 28),
            _tok("30.06.2025", 405, 433, 28),
            _tok("31.03.2026", 490, 518, 28),
        ],
    )
    page = Page(0, 600, 800, [type_row, date_row], 3, column_edges=edges)

    periods = {p.column: p for p in detect_periods(page)}
    assert periods[0].end_date == date(2026, 6, 30)
    assert periods[0].period_type == "quarter"
    assert periods[1].end_date == date(2025, 6, 30)
    assert periods[2].end_date == date(2026, 3, 31)
    assert periods[2].period_type == "year"


def test_detect_periods_ignores_unaligned_date_in_title():
    # A date embedded in a title (far from the column edges) must not become or
    # override a column period.
    edges = [348.0, 433.0, 518.0]
    title = Row(
        0, 5, 13, "Results for the quarter ended 30 June 2026", 48,
        is_header=True,
        tokens=[
            _tok("Results", 48, 90, 5), _tok("for", 91, 105, 5),
            _tok("the", 106, 125, 5), _tok("quarter", 126, 160, 5),
            _tok("ended", 161, 185, 5), _tok("30", 186, 200, 5),
            _tok("June", 201, 230, 5), _tok("2026", 231, 260, 5),
        ],
    )
    date_row = Row(
        0, 28, 36, "30.06.2026 31.03.2026", 320,
        tokens=[
            _tok("30.06.2026", 320, 348, 28),
            _tok("31.03.2026", 490, 518, 28),
        ],
    )
    page = Page(0, 600, 800, [title, date_row], 3, column_edges=edges)
    periods = {p.column: p.end_date for p in detect_periods(page)}
    # Only the two aligned dates are picked up; the title date is ignored.
    assert periods == {0: date(2026, 6, 30), 2: date(2026, 3, 31)}
