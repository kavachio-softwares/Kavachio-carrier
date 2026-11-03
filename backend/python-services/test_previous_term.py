"""A renewed contract's PREVIOUS TERM (19 Oct decision).

A broker can still send the months of the old term that never came in, under
the old contract; the renewal stays the default for its own months. The
calendar keeps the old term's months (they used to be deleted as stale once
the renewal became the newest contract), and a previous term uses the layout
agreed for its renewal chain.
"""
from datetime import date

import pytest


def _mem_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import db
    eng = create_engine("sqlite:///:memory:")
    db.Base.metadata.create_all(
        eng, tables=[db.Program.__table__, db.Contract.__table__,
                     db.ExportTemplate.__table__, db.Pipeline.__table__])
    return sessionmaker(bind=eng)()


@pytest.fixture
def chain():
    """2024 contract, renewed by a 2025 one."""
    import db
    s = _mem_session()
    p = db.Program(tenant_id=1, name="Acme", bdx_frequency="monthly"); s.add(p); s.flush()
    old = db.Contract(program_id=p.id, tenant_id=1, broker_party_id=7, name="2024",
                      inception_dt=date(2024, 1, 1), expiry_dt=date(2025, 1, 1),
                      lifecycle="superseded", status="superseded")
    s.add(old); s.flush()
    new = db.Contract(program_id=p.id, tenant_id=1, broker_party_id=7, name="2025",
                      inception_dt=date(2025, 1, 1), expiry_dt=date(2026, 1, 1),
                      lifecycle="active", status="active", renews_contract_id=old.id)
    alone = db.Contract(program_id=p.id, tenant_id=1, broker_party_id=7, name="other",
                        inception_dt=date(2024, 6, 1), expiry_dt=date(2025, 6, 1))
    s.add_all([new, alone]); s.flush()
    yield s, p, old, new, alone
    s.close()


def test_both_terms_of_a_chain_answer_for_their_own_months(chain):
    import renewal_chain as rc
    s, _p, old, new, alone = chain
    assert rc.own_term(s, old.id) is old and rc.own_term(s, new.id) is new
    assert rc.own_term(s, alone.id) is None            # not renewed: no term limit
    aug24, feb25 = rc.period_start("2024-08"), rc.period_start("2025-02")
    assert rc.covers(old, aug24) and not rc.covers(old, feb25)
    assert rc.covers(new, feb25) and not rc.covers(new, aug24)


def test_calendar_starts_at_the_first_term_not_the_renewal(chain):
    """Anchored on the renewal alone, the 2024 months nobody sent were
    deleted as stale — and could never be sent late."""
    from submission_calendar_service import _chain_inception
    s, _p, old, new, alone = chain
    assert _chain_inception(s, new) == date(2024, 1, 1)
    assert _chain_inception(s, old) == date(2024, 1, 1)
    assert _chain_inception(s, alone) == date(2024, 6, 1)


def test_previous_term_uses_its_chains_layout(chain, monkeypatch):
    import os
    import db
    # The module builds an AI client at import; a placeholder key is enough.
    if not os.getenv("GEMINI_API_KEY"):
        monkeypatch.setenv("GEMINI_API_KEY", "test-placeholder")
    from output_template_routes import _resolve, EXACT
    s, p, old, new, alone = chain
    t = db.ExportTemplate(tenant_id=1, name="layout", contract_id=new.id,
                          is_active=True, structure={})
    s.add(t); s.flush()
    assert _resolve(s, 1, None, p.id, 7, old.id) == (t, EXACT)
    # Never sideways: a contract outside the chain does not borrow it.
    hit, _lvl = _resolve(s, 1, None, p.id, 7, alone.id)
    assert hit is None or hit.id != t.id
