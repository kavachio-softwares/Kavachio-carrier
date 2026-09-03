"""Authorization tests for the carrier-centric chain."""
import os
from fastapi.testclient import TestClient
import main
from db import SessionLocal, Tenant, Party, Program, ProgramBroker, Contract, AppUser
from auth_tokens import mint_access_token

client = TestClient(main.app)

def seed():
    with SessionLocal() as s:
        c1 = Tenant(tenant_name="carrier1", legal_name="Carrier One")
        c2 = Tenant(tenant_name="carrier2", legal_name="Carrier Two")
        s.add_all([c1, c2]); s.commit()
        b1 = Party(tenant_id=c1.id, party_type="broker", legal_name="Broker A", reference="b1")
        b2 = Party(tenant_id=c1.id, party_type="broker", legal_name="Broker B", reference="b2")
        s.add_all([b1, b2]); s.commit()
        p1 = Program(tenant_id=c1.id, name="GL 2026", is_app_managed=True)
        p2 = Program(tenant_id=c2.id, name="Other carrier prog", is_app_managed=True)
        s.add_all([p1, p2]); s.commit()
        s.add(ProgramBroker(tenant_id=c1.id, program_id=p1.id, broker_party_id=b1.id, status="active"))
        s.commit()
        ct = Contract(program_id=p1.id, tenant_id=c1.id, broker_party_id=b1.id,
                      filename="c.pdf", status="active")
        s.add(ct); s.commit()
        ua = AppUser(tenant_id=c1.id, email="a@c1.com", full_name="CarrierAdmin", role="carrier_admin")
        ub = AppUser(tenant_id=None, email="b@b1.com", full_name="BrokerAdmin", role="broker_admin",
                     broker_party_id=b1.id)
        ub2 = AppUser(tenant_id=None, email="b@b2.com", full_name="BrokerB", role="broker_admin",
                      broker_party_id=b2.id)
        uc2 = AppUser(tenant_id=c2.id, email="a@c2.com", full_name="Carrier2Admin", role="carrier_admin")
        s.add_all([ua, ub, ub2, uc2]); s.commit()
        return dict(c1=c1.id, c2=c2.id, b1=b1.id, b2=b2.id, p1=p1.id, p2=p2.id,
                    ct=ct.id, ua=ua.id, ub=ub.id, ub2=ub2.id, uc2=uc2.id)

# The rule-generation subsystem's validation_rule schema is applied by migration
# SQL, not by the model's create_all — give the test DB those columns so the
# contract-detail endpoint can run (this mismatch predates the v4 migration).
def _subsystem_columns():
    from db import engine
    extra = ["rule_engine TEXT", "rule_description TEXT", "validation_stage TEXT",
             "severity TEXT", "canonical_target TEXT", "rule_spec TEXT",
             "error_message TEXT", "source_clause_id INTEGER",
             "source_verbatim_text TEXT", "source_page_number INTEGER",
             "generation_confidence REAL", "rule_status TEXT",
             "contract_id INTEGER", "program_id INTEGER", "rule_class_id INTEGER"]
    with engine.begin() as c:
        for col in extra:
            try: c.exec_driver_sql(f"ALTER TABLE validation_rule ADD COLUMN {col}")
            except Exception: pass
_subsystem_columns()

D = seed()
def H(uid, tid, role): return {"Authorization": "Bearer " + mint_access_token(uid, tid, role)}
CARRIER1 = H(D["ua"], D["c1"], "carrier_admin")
CARRIER2 = H(D["uc2"], D["c2"], "carrier_admin")
BROKER1  = H(D["ub"], None, "broker_admin")
BROKER2  = H(D["ub2"], None, "broker_admin")

fails = []
def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got}, want {want}")
    if not ok: fails.append(label)

C, P, B, T = D["c1"], D["p1"], D["b1"], D["ct"]
chain = f"/carriers/{C}/programs/{P}/brokers/{B}/contracts/{T}"

print("\n-- carrier reaches its own chain --")
check("GET /carriers", client.get("/carriers", headers=CARRIER1).status_code, 200)
check("GET carrier", client.get(f"/carriers/{C}", headers=CARRIER1).status_code, 200)
check("GET programs", client.get(f"/carriers/{C}/programs", headers=CARRIER1).status_code, 200)
check("GET program", client.get(f"/carriers/{C}/programs/{P}", headers=CARRIER1).status_code, 200)
check("GET brokers", client.get(f"/carriers/{C}/programs/{P}/brokers", headers=CARRIER1).status_code, 200)
check("GET contracts", client.get(f"/carriers/{C}/programs/{P}/brokers/{B}/contracts",
                                  headers=CARRIER1).status_code, 200)
check("GET contract", client.get(chain, headers=CARRIER1).status_code, 200)
check("GET policies", client.get(chain + "/policies", headers=CARRIER1).status_code, 200)

print("\n-- a carrier cannot widen scope to another carrier --")
check("carrier2 -> carrier1's id", client.get(f"/carriers/{C}", headers=CARRIER2).status_code, 404)
check("carrier2 -> carrier1's programs",
      client.get(f"/carriers/{C}/programs", headers=CARRIER2).status_code, 404)
check("carrier1 -> carrier2's program (wrong parent)",
      client.get(f"/carriers/{C}/programs/{D['p2']}", headers=CARRIER1).status_code, 404)

print("\n-- broker sees only what the carrier granted --")
check("broker1 lists carriers", client.get("/carriers", headers=BROKER1).status_code, 200)
check("broker1 reaches granted carrier",
      client.get(f"/carriers/{C}", headers=BROKER1).status_code, 200)
check("broker1 reaches its contracts",
      client.get(f"/carriers/{C}/programs/{P}/brokers/{B}/contracts", headers=BROKER1).status_code, 200)
check("broker2 (not on programme) -> carrier",
      client.get(f"/carriers/{C}", headers=BROKER2).status_code, 404)
check("broker1 cannot address broker2",
      client.get(f"/carriers/{C}/programs/{P}/brokers/{D['b2']}/contracts",
                 headers=BROKER1).status_code, 404)

print("\n-- contract must hang off the named broker --")
check("contract under wrong broker",
      client.get(f"/carriers/{C}/programs/{P}/brokers/{D['b2']}/contracts/{T}",
                 headers=CARRIER1).status_code, 404)
check("contract under wrong programme",
      client.get(f"/carriers/{C}/programs/{D['p2']}/brokers/{B}/contracts/{T}",
                 headers=CARRIER1).status_code, 404)

print("\n-- unauthenticated --")
check("no token", client.get(f"/carriers/{C}/programs").status_code, 401)

print("\n-- deprecated flat alias still works --")
check("flat /programs?mga=", client.get("/programs?mga=carrier1", headers=CARRIER1).status_code, 200)

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED: ' + ', '.join(fails)}")
raise SystemExit(1 if fails else 0)
