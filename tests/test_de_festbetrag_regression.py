"""PDF text may concatenate adjacent money columns without whitespace."""

import pytest

from app.adapters.de_festbetrag import LINE_RE


@pytest.mark.parametrize(
    "line,expected",
    [
        (
            "Nilotinib 150 0,2 392 KAPS 16.617,67 5.125,9711.491,70 TASIGNA 150MG 10918437",
            ("16.617,67", "5.125,97", "11.491,70", "TASIGNA 150MG", "10918437"),
        ),
        (
            "360 360 300 FTBL 10.931,30 372,3710.558,93 EXJADE 360MG FTA 12868608",
            ("10.931,30", "372,37", "10.558,93", "EXJADE 360MG FTA", "12868608"),
        ),
        (
            "Nilotinib 150 0,2 392 KAPS 16.617,675.125,9711.491,70 TASIGNA 150MG 10918437",
            ("16.617,67", "5.125,97", "11.491,70", "TASIGNA 150MG", "10918437"),
        ),
        (
            "360 360 300 FTBL 10.931,30 372,37 10.558,93 EXJADE 360MG FTA 12868608",
            ("10.931,30", "372,37", "10.558,93", "EXJADE 360MG FTA", "12868608"),
        ),
    ],
)
def test_adjacent_money_preserves_all_columns(line, expected):
    match = LINE_RE.fullmatch(line)
    assert match
    assert tuple(match[g] for g in ("preis", "festbetrag", "diff", "name", "pzn")) == expected
    assert match["pack"] in {"392", "300"}
    assert match["form"] in {"KAPS", "FTBL"}
