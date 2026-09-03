"""Scenarios Q–X of the mapping prompt, run without a network or a database.

Each one is a claim about when a column may be wired to a field WITHOUT asking a
person, and when it must not be.
"""
import sys
sys.path.insert(0, ".")

from semantic_mapping import (
    AUTO_MAPPED, REVIEW_REQUIRED, MANUALLY_CONFIRMED, ALIAS, EXACT, MANUAL,
    NORMALIZED, SEMANTIC, Decision, resolve_field, resolve_sheet,
    unresolved_required, value_kind, compatible, min_confidence,
)

fails = []


def check(label, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        fails.append(label)


MONEY = ["1500.00", "2300.50", "980", "12,400.00", "75.25"]
WORDS = ["Commercial", "Personal", "Commercial", "Marine", "Commercial"]
DATES = ["2026-01-01", "2026-02-15", "2026-03-30", "2026-04-01", "2026-05-05"]

COMMISSION = {"field_key": "commission", "display_name": "Commission",
              "column_name": "Commission", "data_type": "decimal", "required": True}


def field(**over):
    f = dict(COMMISSION)
    f.update(over)
    return f


print(f"threshold in force: {min_confidence():.0%}\n")

print("Q. exact name")
d = resolve_field(field(), ["Policy No", "Commission", "GWP"],
                  {"Commission": MONEY})
check("auto-mapped", d.status == AUTO_MAPPED and d.source == "Commission",
      f"{d.status} via {d.method}")
check("recorded as EXACT", d.method == EXACT, str(d.method))

print("\nR. semantic — 'Commission Amount'")
d = resolve_field(field(), ["Policy No", "Commission Amount"],
                  {"Commission Amount": MONEY})
check("auto-mapped", d.status == AUTO_MAPPED and d.source == "Commission Amount",
      f"{d.status} via {d.method} @ {d.confidence:.0%}")
check("clears the bar", d.confidence >= min_confidence(), f"{d.confidence:.3f}")

print("\nS. abbreviation — 'CM'")
d = resolve_field(field(), ["Policy No", "CM", "GWP"], {"CM": MONEY})
check("auto-mapped", d.status == AUTO_MAPPED and d.source == "CM",
      f"{d.status} via {d.method} @ {d.confidence:.0%}")
check("recorded as ALIAS", d.method == ALIAS, str(d.method))
check("says why in plain words", "known way of writing" in d.reason, d.reason)

print("\nT. low confidence — 'Cost'")
d = resolve_field(field(), ["Policy No", "Cost"], {"Cost": MONEY},
                  semantic={"source": "Cost", "confidence": 0.72})
check("NOT auto-mapped", d.status == REVIEW_REQUIRED, d.status)
check("nothing was wired up", d.source is None, str(d.source))
check("keeps the score for the reviewer", 0.7 <= d.confidence <= 0.75,
      f"{d.confidence:.2f}")
check("names the shortfall", "under the" in d.reason, d.reason)

print("\nU. several candidates — CM, Comm, Cost")
d = resolve_field(field(), ["CM", "Comm", "Cost"],
                  {"CM": MONEY, "Comm": MONEY, "Cost": MONEY})
check("sent to review, not guessed", d.status == REVIEW_REQUIRED, d.status)
check("lists what it was torn between", "more than one column" in d.reason, d.reason)
check("offers them with scores", len(d.candidates) >= 2,
      str([(c["source"], c["confidence"]) for c in d.candidates]))

print("\nV. right name, wrong data — 'CM' holding words")
d = resolve_field(field(), ["Policy No", "CM"], {"CM": WORDS})
check("rejected despite the name", d.status == REVIEW_REQUIRED, d.status)
check("nothing was wired up", d.source is None, str(d.source))
check("says the data is the problem", "text values" in d.reason, d.reason)

print("\nW. the display name is renamed")
d = resolve_field(
    field(display_name="Commission Amount"),   # renamed in the editor
    ["Policy No", "CM"], {"CM": MONEY})
check("still finds CM", d.status == AUTO_MAPPED and d.source == "CM",
      f"{d.status}/{d.source}")
check("still keyed on the internal field", d.field_key == "commission", d.field_key)

print("\nX. a required field with nowhere to come from")
ds = resolve_sheet(
    [field(), {"field_key": "policy_number", "display_name": "Policy Number",
              "column_name": "Policy Number", "data_type": "string", "required": True}],
    ["Policy No", "Premium", "Fees"],
    {"Policy No": ["P1", "P2"], "Premium": MONEY, "Fees": MONEY})
missing = unresolved_required(ds)
check("policy number was still found by alias",
      any(d.field_key == "policy_number" and d.mapped for d in ds),
      str([(d.field_key, d.status, d.source) for d in ds]))
check("commission is reported unresolved",
      [d.field_key for d in missing] == ["commission"],
      str([d.field_key for d in missing]))
check("and it is not silently blank", all(d.source is None for d in missing))

print("\nExtra — an explicit mapping outranks everything")
d = resolve_field(field(), ["CM", "Commission"], {"CM": MONEY, "Commission": MONEY},
                  existing_source="CM")
check("kept the configured column", d.source == "CM" and d.method == MANUAL,
      f"{d.source}/{d.method}")
check("marked as confirmed by a person", d.status == MANUALLY_CONFIRMED, d.status)

print("\nExtra — value kinds are read off the values")
check("money is a number", value_kind(MONEY) == "number", value_kind(MONEY))
check("words are text", value_kind(WORDS) == "text", value_kind(WORDS))
check("dates are dates", value_kind(DATES) == "date", value_kind(DATES))
check("no samples decides nothing", value_kind([]) == "unknown")
check("no samples never blocks a mapping", compatible("decimal", [])[0])
check("a date column cannot feed a decimal", not compatible("decimal", DATES)[0])
check("a number CAN feed a string field", compatible("string", MONEY)[0])

print("\nExtra — the decision is auditable")
d = resolve_field(field(), ["CM"], {"CM": MONEY})
rec = d.to_dict()
for k in ("field_key", "source", "method", "confidence", "status",
          "version", "alias_version", "candidates"):
    check(f"records {k}", k in rec, str(rec.get(k)))

print("\n" + ("ALL PASS" if not fails else f"FAILURES ({len(fails)}): {fails}"))
sys.exit(1 if fails else 0)
