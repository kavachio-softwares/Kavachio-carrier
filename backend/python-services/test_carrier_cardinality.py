"""carrier 1─N program  N─M broker  1─N contract  1─N policy.

The same broker is deliberately put on TWO programmes of one carrier AND on a
programme of a SECOND carrier — the shape the user described.
"""
from fastapi.testclient import TestClient
import main
from db import (SessionLocal, CanonicalSession, Tenant, Party, Program,
                ProgramBroker, Contract, AppUser)
from auth_tokens import mint_access_token
from canonical import CANONICAL_TABLES
from sqlalchemy import insert

client = TestClient(main.app)

with SessionLocal() as s:
    c1 = Tenant(tenant_name="cx1", legal_name="Carrier X"); c2 = Tenant(tenant_name="cx2", legal_name="Carrier Y")
    s.add_all([c1, c2]); s.commit()
    bk = Party(tenant_id=c1.id, party_type="broker", legal_name="Shared Broker", reference="shared")
    other = Party(tenant_id=c1.id, party_type="broker", legal_name="Other Broker", reference="other")
    s.add_all([bk, other]); s.commit()
    # carrier X has TWO programmes; carrier Y has one. Same broker on all three.
    pA = Program(tenant_id=c1.id, name="X-Prog-A", is_app_managed=True)
    pB = Program(tenant_id=c1.id, name="X-Prog-B", is_app_managed=True)
    pC = Program(tenant_id=c2.id, name="Y-Prog-C", is_app_managed=True)
    s.add_all([pA, pB, pC]); s.commit()
    for (t, pr) in ((c1, pA), (c1, pB), (c2, pC)):
        s.add(ProgramBroker(tenant_id=t.id, program_id=pr.id, broker_party_id=bk.id, status="active"))
    s.add(ProgramBroker(tenant_id=c1.id, program_id=pA.id, broker_party_id=other.id, status="active"))
    s.commit()
    # broker holds contracts on A and B; plus an UNASSIGNED carrier contract on A
    ctA = Contract(program_id=pA.id, tenant_id=c1.id, broker_party_id=bk.id, filename="A.pdf", status="active")
    ctB = Contract(program_id=pB.id, tenant_id=c1.id, broker_party_id=bk.id, filename="B.pdf", status="active")
    ctU = Contract(program_id=pA.id, tenant_id=c1.id, broker_party_id=None, filename="unassigned.pdf", status="active")
    s.add_all([ctA, ctB, ctU]); s.commit()
    ub = AppUser(tenant_id=None, email="sb@x.com", full_name="SB", role="broker_admin", broker_party_id=bk.id)
    ua = AppUser(tenant_id=c1.id, email="ca@x.com", full_name="CA", role="carrier_admin")
    s.add_all([ub, ua]); s.commit()
    D = dict(c1=c1.id, c2=c2.id, bk=bk.id, other=other.id, pA=pA.id, pB=pB.id, pC=pC.id,
             ctA=ctA.id, ctB=ctB.id, ctU=ctU.id, ub=ub.id, ua=ua.id)

# contract A has THREE policies, contract B has one
pol = CANONICAL_TABLES["policy"]
with CanonicalSession() as cs:
    for n in ("PA-1", "PA-2", "PA-3"):
        cs.execute(insert(pol).values(policy_number=n, policy_contract_id=D["ctA"], tenant_id=D["c1"]))
    cs.execute(insert(pol).values(policy_number="PB-1", policy_contract_id=D["ctB"], tenant_id=D["c1"]))
    cs.commit()

BROKER = {"Authorization": "Bearer " + mint_access_token(D["ub"], None, "broker_admin")}
CARRIER = {"Authorization": "Bearer " + mint_access_token(D["ua"], D["c1"], "carrier_admin")}

fails = []
def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got}" + ("" if ok else f"  (want {want})"))
    if not ok: fails.append(label)

def ids(r): 
    j = r.json(); items = j.get("items", j) if isinstance(j, dict) else j
    return sorted(c["id"] for c in items)

print("\n-- carrier 1─N programs --")
check("carrier X programs", len(client.get(f"/carriers/{D['c1']}/programs", headers=CARRIER).json()), 2)

print("\n-- program N─M broker: same broker on many programs, across carriers --")
check("broker sees both carriers", len(client.get("/carriers", headers=BROKER).json()), 2)
check("broker on X/Prog-A", client.get(f"/carriers/{D['c1']}/programs/{D['pA']}/brokers/{D['bk']}/contracts", headers=BROKER).status_code, 200)
check("broker on X/Prog-B", client.get(f"/carriers/{D['c1']}/programs/{D['pB']}/brokers/{D['bk']}/contracts", headers=BROKER).status_code, 200)
check("same broker on Y/Prog-C", client.get(f"/carriers/{D['c2']}/programs/{D['pC']}/brokers/{D['bk']}/contracts", headers=BROKER).status_code, 200)

print("\n-- broker 1─N contracts, scoped per programme (no bleed) --")
check("A lists only A's contract", ids(client.get(f"/carriers/{D['c1']}/programs/{D['pA']}/brokers/{D['bk']}/contracts", headers=BROKER)), [D["ctA"]])
check("B lists only B's contract", ids(client.get(f"/carriers/{D['c1']}/programs/{D['pB']}/brokers/{D['bk']}/contracts", headers=BROKER)), [D["ctB"]])
check("contract B not reachable under programme A",
      client.get(f"/carriers/{D['c1']}/programs/{D['pA']}/brokers/{D['bk']}/contracts/{D['ctB']}", headers=BROKER).status_code, 404)

print("\n-- unassigned carrier contract: carrier sees it, broker must not --")
check("carrier sees unassigned", sorted(ids(client.get(f"/carriers/{D['c1']}/programs/{D['pA']}/brokers/{D['bk']}/contracts", headers=CARRIER))), sorted([D["ctA"], D["ctU"]]))
check("broker cannot fetch unassigned", client.get(f"/carriers/{D['c1']}/programs/{D['pA']}/brokers/{D['bk']}/contracts/{D['ctU']}", headers=BROKER).status_code, 404)

print("\n-- contract 1─N policies --")
rA = client.get(f"/carriers/{D['c1']}/programs/{D['pA']}/brokers/{D['bk']}/contracts/{D['ctA']}/policies", headers=BROKER)
check("contract A has 3 policies", rA.json()["total"], 3)
rB = client.get(f"/carriers/{D['c1']}/programs/{D['pB']}/brokers/{D['bk']}/contracts/{D['ctB']}/policies", headers=BROKER)
check("contract B has 1 policy", rB.json()["total"], 1)
pid = rA.json()["items"][0]["policy_id"]
check("policy detail under its contract", client.get(f"/carriers/{D['c1']}/programs/{D['pA']}/brokers/{D['bk']}/contracts/{D['ctA']}/policies/{pid}", headers=BROKER).status_code, 200)
check("policy NOT reachable under contract B", client.get(f"/carriers/{D['c1']}/programs/{D['pB']}/brokers/{D['bk']}/contracts/{D['ctB']}/policies/{pid}", headers=BROKER).status_code, 404)

print("\n-- one broker cannot address another on a shared programme --")
check("broker -> other broker's path", client.get(f"/carriers/{D['c1']}/programs/{D['pA']}/brokers/{D['other']}/contracts", headers=BROKER).status_code, 404)

print(f"\n{'ALL PASS' if not fails else str(len(fails))+' FAILED: '+', '.join(fails)}")
raise SystemExit(1 if fails else 0)
