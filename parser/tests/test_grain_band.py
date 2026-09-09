"""The year-grouping-band grain guard: a 'FY2024' label printed over the first
month of a 12-month block must not make that month annual (which made the
executor sum all 12 months into January — the 29,132 incident). A standalone
FY summary column, never part of a monthly run, must keep its annual grain."""

from app.datamodel.derive import _monthly_run_cols


def _months(start_col, year, month, n):
    """n consecutive monthly ISO dates starting at (year, month), keyed by column."""
    out = {}
    y, m = year, month
    for i in range(n):
        out[start_col + i] = f"{y:04d}-{m:02d}-28"
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def test_band_month_is_inside_the_run():
    # Jan..Dec 2024 across cols 5..16; the Jan column (5) carries the FY band.
    col_date = _months(5, 2024, 1, 12)
    run = _monthly_run_cols(col_date)
    assert 5 in run and 16 in run          # every month of the block is in the run
    assert run == set(range(5, 17))


def test_standalone_annual_column_is_not_in_a_run():
    # A single FY-total column carrying a year-end date, no neighbours.
    col_date = {41: "2024-12-31"}
    assert _monthly_run_cols(col_date) == set()


def test_annual_column_adjacent_to_a_block_stays_out_of_the_run():
    # 12 monthly columns, then a lone FY column two columns to the right.
    col_date = _months(5, 2024, 1, 12)
    col_date[19] = "2024-12-31"            # gap at 17,18 → not consecutive
    run = _monthly_run_cols(col_date)
    assert set(range(5, 17)) <= run
    assert 19 not in run                   # the standalone FY column is untouched


def test_two_month_stub_is_not_a_run():
    # Fewer than min_run consecutive months is not treated as a run.
    assert _monthly_run_cols({5: "2024-01-31", 6: "2024-02-29"}) == set()


def test_quarterly_dates_do_not_form_a_monthly_run():
    col_date = {5: "2024-03-31", 6: "2024-06-30", 7: "2024-09-30", 8: "2024-12-31"}
    assert _monthly_run_cols(col_date) == set()
