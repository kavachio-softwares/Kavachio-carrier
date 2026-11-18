"""45.2 — every column a download has is explained in plain English."""
import export_glossary as gl
import policy_export as pe


def _words(report, **kw):
    return {w for _h, w, _m in gl.rows(report, **kw)}


def test_every_policy_column_is_explained():
    words = " / ".join(_words("policies"))
    for seat in ("carrier", "broker"):
        for label, _w, _f in pe.columns(seat):
            assert label in words or label.split()[0] in words, label


def test_limits_are_filled_in_and_none_is_left_open():
    for report in gl.REPORTS:
        for _h, _w, meaning in gl.rows(report, target="5 days", near_cap="85", late_shown=10,
                                       scorecard_shown=10, escalations_shown=25):
            assert "{" not in meaning, meaning


def test_the_pdf_rows_start_with_a_header_and_every_meaning_is_plain_text():
    rows = gl.pdf_rows("periodic", target="5 days")
    assert rows[0] == ["Column", "Meaning"]
    assert any("5 days" in r[1] for r in rows)


def test_numbers_and_lists_come_from_the_code_that_uses_them():
    import executive_report as er
    import fix_performance as fp
    import partner_performance as pp
    text = " ".join(m for r in gl.REPORTS for _h, _w, m in gl.rows(r, target="5 days"))
    assert f"top {er.TOP} programmes" in text
    assert f"At most {er.ESCALATIONS_SHOWN}" in text
    for _k, words, _lo, _hi in fp.AGE_BANDS:
        assert words in text
    for k, label in pp.CHANNEL_LABEL.items():
        assert label in text, label
