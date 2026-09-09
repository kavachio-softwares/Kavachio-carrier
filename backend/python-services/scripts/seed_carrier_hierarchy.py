"""
Seed one carrier hierarchy so the Phase-1 flow can be clicked through.

Neutral names on purpose — this is a shape to develop against, not a pretend
customer. It builds exactly one of each thing the hierarchy needs:

    Northwind Insurance          carrier (tenant)
      ├── carrier.admin@…        carrier_admin
      ├── Programme A ─┬─ Bridge Brokers      contract, carrier-uploaded  → approved
      │                └─ Coastal Brokers     contract, broker-uploaded   → pending
      └── Programme B ─── Bridge Brokers      (same broker, second programme)

The two contracts are deliberately different: one shows a carrier upload going
live immediately, the other shows a broker upload waiting in the approvals
queue. Bridge Brokers sits on BOTH programmes, which is the many-to-many the
whole hierarchy exists for.

Re-runnable: it finds-or-creates every row, so a second run changes nothing.

    python scripts/seed_carrier_hierarchy.py
    python scripts/seed_carrier_hierarchy.py --reset   # delete the seed first
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402
from db import (  # noqa: E402
    SessionLocal, Party, Program, Contract, AppUser, ProgramBroker, ContractApproval,
)

CARRIER = "Northwind Insurance"
SEED_TAG = "seed:carrier-hierarchy"   # so --reset can find exactly what we made


def _pw(raw: str) -> str:
    """Hash a password the same way the app does, so these logins really work."""
    try:
        from auth_utils import hash_password
        return hash_password(raw)
    except Exception:
        import bcrypt
        return bcrypt.hashpw(raw.encode(), bcrypt.gensalt()).decode()


def reset(s) -> None:
    tid = s.execute(text("SELECT tenant_id FROM tenant WHERE tenant_name = :n"),
                    {"n": CARRIER}).scalar()
    if not tid:
        print("nothing to reset")
        return
    # Children first — the FKs are real.
    s.execute(text("DELETE FROM contract_approval WHERE tenant_id = :t"), {"t": tid})
    s.execute(text("DELETE FROM contract WHERE tenant_id = :t"), {"t": tid})
    s.execute(text("DELETE FROM program_broker WHERE tenant_id = :t"), {"t": tid})
    s.execute(text("DELETE FROM program WHERE tenant_id = :t"), {"t": tid})
    s.execute(text("DELETE FROM app_user WHERE tenant_id = :t OR broker_party_id IN "
                   "(SELECT party_id FROM party WHERE tenant_id = :t)"), {"t": tid})
    s.execute(text("DELETE FROM party WHERE tenant_id = :t"), {"t": tid})
    s.execute(text("DELETE FROM tenant WHERE tenant_id = :t"), {"t": tid})
    s.commit()
    print(f"reset: removed carrier {CARRIER} (tenant {tid}) and everything under it")


def main(do_reset: bool = False) -> None:
    with SessionLocal() as s:
        if do_reset:
            reset(s)

        # --- the carrier ----------------------------------------------------
        tid = s.execute(text("SELECT tenant_id FROM tenant WHERE tenant_name = :n"),
                        {"n": CARRIER}).scalar()
        if not tid:
            tid = s.execute(
                text("INSERT INTO tenant (tenant_name, tenant_type, is_active) "
                     "VALUES (:n, 'carrier', true) RETURNING tenant_id"),
                {"n": CARRIER},
            ).scalar()
            s.commit()
        print(f"carrier  : {CARRIER}  (tenant_id={tid})")

        # --- the carrier's admin -------------------------------------------
        # Every login except the very first Kavachio account has to record who
        # let it in (the enforce_invitation_chain trigger). A carrier admin is
        # invited by the platform, so the seed needs a platform account to
        # point at — the same chain a real sign-up walks.
        platform = (s.query(AppUser)
                      .filter(AppUser.role == "kavachio_admin")
                      .order_by(AppUser.id).first())
        if not platform:
            raise SystemExit(
                "No kavachio_admin exists. The platform account is the root of the\n"
                "invitation chain, so create it before seeding a carrier."
            )

        admin = s.query(AppUser).filter(AppUser.email == "carrier.admin@northwind.test").first()
        if not admin:
            admin = AppUser(
                tenant_id=tid, email="carrier.admin@northwind.test",
                full_name="Carrier Admin", role="carrier_admin", status="active",
                password=_pw("Passw0rd!"), invited_by_user_id=platform.id,
                accepted_at=datetime.now(timezone.utc),
            )
            s.add(admin); s.commit()
        print(f"admin    : {admin.email}  (user_id={admin.id})  password: Passw0rd!")

        # --- two brokers ----------------------------------------------------
        brokers = {}
        for name in ("Bridge Brokers", "Coastal Brokers"):
            b = (s.query(Party)
                   .filter(Party.tenant_id == tid, Party.legal_name == name).first())
            if not b:
                b = Party(tenant_id=tid, party_type="broker", legal_name=name,
                          is_app_managed=True, is_active=True, notes=SEED_TAG)
                s.add(b); s.commit()
            brokers[name] = b
            print(f"broker   : {name}  (party_id={b.id})")

        # --- a broker admin, so the broker side has a real person ------------
        bu = s.query(AppUser).filter(AppUser.email == "admin@coastalbrokers.test").first()
        if not bu:
            bu = AppUser(
                tenant_id=None,                       # a broker seat has NO carrier
                broker_party_id=brokers["Coastal Brokers"].id,
                email="admin@coastalbrokers.test", full_name="Coastal Broker Admin",
                role="broker_admin", status="active", password=_pw("Passw0rd!"),
                invited_by_user_id=admin.id, accepted_at=datetime.now(timezone.utc),
            )
            s.add(bu); s.commit()
        print(f"broker user: {bu.email}  (user_id={bu.id}, broker_party_id={bu.broker_party_id})")

        # --- two programmes -------------------------------------------------
        progs = {}
        for name in ("Programme A", "Programme B"):
            p = (s.query(Program)
                   .filter(Program.tenant_id == tid, Program.name == name).first())
            if not p:
                p = Program(tenant_id=tid, name=name, status="active",
                            is_app_managed=True, bdx_frequency="monthly")
                s.add(p); s.commit()
            progs[name] = p
            print(f"programme: {name}  (program_id={p.id})")

        # --- who is on what -------------------------------------------------
        # Bridge is on BOTH programmes; Coastal only on A. That asymmetry is the
        # point: it proves the link table, not a column on either side.
        pairs = [("Programme A", "Bridge Brokers"),
                 ("Programme A", "Coastal Brokers"),
                 ("Programme B", "Bridge Brokers")]
        for pname, bname in pairs:
            prog, brk = progs[pname], brokers[bname]
            link = (s.query(ProgramBroker)
                      .filter(ProgramBroker.program_id == prog.id,
                              ProgramBroker.broker_party_id == brk.id).first())
            if not link:
                s.add(ProgramBroker(tenant_id=tid, program_id=prog.id,
                                    broker_party_id=brk.id, status="active",
                                    assigned_by_user_id=admin.id))
                s.commit()
            print(f"on       : {bname} -> {pname}")

        # --- two contracts on two brokers ------------------------------------
        # There used to be one of each APPROVAL path here: a carrier upload that
        # was live on arrival and a broker upload that waited for the carrier to
        # let it in. The gate and the broker-side upload were removed together,
        # so both contracts are now simply the carrier's.
        def contract(prog, brk, filename, cap):
            c = (s.query(Contract)
                   .filter(Contract.program_id == prog.id,
                           Contract.filename == filename).first())
            if c:
                return c
            now = datetime.now(timezone.utc)
            c = Contract(
                tenant_id=tid, program_id=prog.id, broker_party_id=brk.id,
                filename=filename, status="extracted",
                inception_dt=date(2026, 1, 1), expiry_dt=date(2026, 12, 31),
                premium_cap_amount=cap, premium_cap_currency="USD",
                submitted_by_user_id=admin.id, submitted_at=now,
            )
            s.add(c); s.commit()
            s.add(ContractApproval(tenant_id=tid, contract_id=c.id, action="submitted",
                                   acted_by_user_id=admin.id, acted_at=now))
            s.commit()
            return c

        c1 = contract(progs["Programme A"], brokers["Bridge Brokers"],
                      "ProgrammeA_Bridge_2026.pdf", 12000000)
        print(f"contract : {c1.filename}  (Bridge Brokers)")

        c2 = contract(progs["Programme A"], brokers["Coastal Brokers"],
                      "ProgrammeA_Coastal_2026.pdf", 5000000)
        print(f"contract : {c2.filename}  (Coastal Brokers)")

        print("\nseed complete — sign in as carrier.admin@northwind.test / Passw0rd!")


if __name__ == "__main__":
    main(do_reset="--reset" in sys.argv)
