from app.raw_extraction.column_utils import column_index, column_letter


def test_column_letter():
    assert column_letter(1) == "A"
    assert column_letter(26) == "Z"
    assert column_letter(27) == "AA"
    assert column_letter(2021) == "BYS"  # the IPV Quarterly_Output far edge
    assert column_letter(0) == ""


def test_round_trip():
    for n in (1, 26, 27, 52, 100, 702, 703, 2021, 16384):
        assert column_index(column_letter(n)) == n
