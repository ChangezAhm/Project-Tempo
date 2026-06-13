"""Tiny helpers for converting between column indices and Excel letters.

Lives in our codebase so the parser doesn't depend on openpyxl for util
conversions after the move to Aspose.Cells.
"""

from __future__ import annotations


def column_letter(col_num_1based: int) -> str:
    """1 → 'A', 27 → 'AA'."""
    if col_num_1based < 1:
        return ""
    s = ""
    n = col_num_1based
    while n > 0:
        n, rem = divmod(n - 1, 26)
        s = chr(65 + rem) + s
    return s


def column_index(col_letters: str) -> int:
    """'A' → 1, 'AA' → 27."""
    n = 0
    for ch in col_letters.upper():
        if "A" <= ch <= "Z":
            n = n * 26 + (ord(ch) - ord("A") + 1)
    return n
