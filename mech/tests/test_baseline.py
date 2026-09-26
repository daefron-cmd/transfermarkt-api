import pytest
from hypothesis import given
from hypothesis import strategies as st

from mech import baseline

HDR = baseline.HEADER + "\n" + baseline.DIVIDER + "\n"
ROW = "| mech.x.f__mutmut_1 | equivalent | codec alias per RFC | | 000000000000 |\n"
GUARDED = (
    "| mech.x.f__mutmut_2 | guarded-elsewhere | encoding pinned by linter "
    "| ruff:PLW1514 | 111111111111 |\n"
)


def test_parse_header_only():
    assert baseline.parse(HDR) == []


def test_parse_round_trip_rows():
    rows = baseline.parse(HDR + ROW + GUARDED)
    assert rows[0] == (
        "mech.x.f__mutmut_1",
        "equivalent",
        "codec alias per RFC",
        "",
        "000000000000",
    )
    assert baseline.parse(baseline.render(rows)) == rows  # B-22


def test_pipe_in_cell_round_trips():
    """A reason legitimately contains `|` (this broke a real MUTANTS.md append
    on 2026-08-02, on `str | None`). validate_row accepts it, so render must
    escape it markdown-style (\\|) and parse must unescape — the three have to
    agree on what a well-formed row is."""
    row = ("m", "equivalent", "uses str | None", "", "0123456789ab")
    assert baseline.validate_row(row) is None
    assert baseline.parse(baseline.render([row])) == [row]


def test_parse_unescapes_hand_written_escaped_pipe():
    """The rendered form is a valid markdown table, so a hand-edited row using
    markdown's own \\| convention must parse to the literal pipe."""
    rows = baseline.parse(
        HDR + "| m | equivalent | uses str \\| None | | 0123456789ab |\n"
    )
    assert rows == [("m", "equivalent", "uses str | None", "", "0123456789ab")]


def test_parse_rejects_bad_header():
    with pytest.raises(baseline.BaselineError):
        baseline.parse("| wrong |\n|---|\n")


def test_parse_crlf_equals_lf():  # B-01 one trailing \r stripped per line
    crlf = (HDR + ROW).replace("\n", "\r\n")
    assert baseline.parse(crlf) == baseline.parse(HDR + ROW)


def test_parse_wrong_header_correct_divider_rejected():
    with pytest.raises(baseline.BaselineError) as ei:
        baseline.parse("| wrong |\n" + baseline.DIVIDER + "\n")
    assert str(ei.value) == "missing or wrong header"


def test_parse_truncated_table_rejected():
    for text in ("", baseline.HEADER + "\n"):
        with pytest.raises(baseline.BaselineError) as ei:
            baseline.parse(text)
        assert str(ei.value) == "missing or wrong header"


def test_parse_row_without_outer_pipes():
    rows = baseline.parse(HDR + "a | b | c | d | e\n")
    assert rows == [("a", "b", "c", "d", "e")]


def test_parse_cell_count_reason_exact():
    line = "| one | two |"
    with pytest.raises(baseline.BaselineError) as ei:
        baseline.parse(HDR + line + "\n")
    assert str(ei.value) == f"row must have exactly 5 cells: {line!r}"


def test_parse_rejects_the_pre_fingerprint_four_cell_row():
    """The migration is one-way: an unstamped row is malformed, not tolerated."""
    line = "| mech.x.f__mutmut_9 | equivalent | codec alias | |"
    with pytest.raises(baseline.BaselineError) as ei:
        baseline.parse(HDR + line + "\n")
    assert str(ei.value) == f"row must have exactly 5 cells: {line!r}"


def test_parse_duplicate_id_reason_exact():
    with pytest.raises(baseline.BaselineError) as ei:
        baseline.parse(HDR + ROW + ROW)
    assert str(ei.value) == "duplicate mutant ID: mech.x.f__mutmut_1"


def test_parse_rejects_duplicate_id():
    with pytest.raises(baseline.BaselineError):
        baseline.parse(HDR + ROW + ROW)


FP = "406497353fdc"


def test_validate_row_rules():
    assert baseline.validate_row(("m", "equivalent", "r", "", FP)) is None
    assert (
        baseline.validate_row(("m", "guarded-elsewhere", "r", "ruff:PLW1514", FP))
        is None
    )
    assert baseline.validate_row(("m", "equivalent", "r", "x", FP))  # 4th must be empty
    # 4th required
    assert baseline.validate_row(("m", "environment-equivalent", "r", "", FP))
    # needs ruff: prefix
    assert baseline.validate_row(("m", "guarded-elsewhere", "r", "PLW1514", FP))
    assert baseline.validate_row(("m", "accepted-gap", "r", "", FP))  # unknown class
    assert baseline.validate_row(("m", "equivalent", "", "", FP))  # empty reason


def test_validate_row_reasons_exact():
    assert baseline.validate_row(("", "equivalent", "r", "", FP)) == "empty mutant ID"
    assert baseline.validate_row(("m", "equivalent", "", "", FP)) == "empty reason"
    assert (
        baseline.validate_row(("m", "equivalent", "r", "x", FP))
        == "equivalent rows must leave the fourth cell empty"
    )
    assert baseline.validate_row(("m", "guarded-elsewhere", "r", "PLW1514", FP)) == (
        "guarded-elsewhere delegate must be ruff:<CODE> (a single rule code)"
    )


def test_validate_row_rejects_a_malformed_fingerprint_cell():
    """Grammar only — whether the hash MATCHES its mutant is a binding check that
    needs the generated tree, which no row-local predicate can see."""
    for bad in ("", "406497353FDC", "406497353fd", "406497353fdc0", "not-hex-here"):
        assert baseline.validate_row(("m", "equivalent", "r", "", bad)) == (
            "mutation fingerprint must be 12 lowercase hex digits"
        )


@pytest.mark.parametrize("cell", range(5))
@pytest.mark.parametrize("control", ["\n", "\r"])
def test_validate_row_rejects_multiline_cells(cell, control):
    row: list[str] = ["m", "equivalent", "reason", "", "0123456789ab"]
    row[cell] += control + "continued"

    candidate = (row[0], row[1], row[2], row[3], row[4])
    assert baseline.validate_row(candidate) == (
        "cells must stay on one line (carriage returns and newlines are invalid)"
    )


def test_validate_row_accepts_a_well_formed_fingerprint():
    assert baseline.validate_row(("m", "equivalent", "r", "", "0123456789ab")) is None


# `|` is deliberately IN the alphabet: the generator excluded it until
# 2026-08-02, which is exactly why the unescaped-pipe round-trip break
# shipped (worklist item 3 / BACKLOG 2).
_cell = (
    st.text(
        alphabet=st.characters(
            blacklist_characters="\r\n", blacklist_categories=("Cs",)
        ),
        min_size=1,
        max_size=30,
    )
    .map(str.strip)
    .filter(bool)
)


_fingerprint = st.text(alphabet="0123456789abcdef", min_size=12, max_size=12)


@given(
    st.lists(
        st.tuples(_cell, st.sampled_from(baseline.CLASSES), _cell, _cell, _fingerprint),
        max_size=8,
    )
)
def test_render_parse_round_trip(rows):  # B-22 property
    """render/parse must round-trip ANY well-formed row.

    The 4th cell used to be blanked when the class was `equivalent`, mimicking a
    rule `validate_row` enforces and `parse` does not. That narrowed the input
    space for no reason and read as exercising a constraint the function under
    test never touches — parse is a delimiter split, and keeping the two
    concerns separate is the point of having both. De-duplication by mutant ID
    stays: render/parse genuinely cannot distinguish two rows sharing an ID.
    """
    fixed = []
    seen = set()
    for mid, cls, reason, cond, fingerprint in rows:
        if mid in seen:
            continue
        seen.add(mid)
        fixed.append((mid, cls, reason, cond, fingerprint))
    assert baseline.parse(baseline.render(fixed)) == fixed
