/**
 * What happens when somebody types over a chip, across the cases that decide
 * whether it is safe to move a term.
 *
 *     node --experimental-strip-types scripts/wording.scenarios.ts
 *
 * The rule being checked: move the term when the slot holds exactly one value
 * and that value is unambiguous; otherwise leave every word alone and let the
 * review say the wording no longer quotes it. Guessing is worse than saying so.
 */
import { chipEdits, readTermMove, restoreChips, type TermShape }
  from "../src/utils/wordingEdits.ts";

const PCT: TermShape = { kind: "percent" };
const MONEY: TermShape = { kind: "money" };
const TEXT: TermShape = { kind: "text" };
const CHOICE: TermShape = { kind: "choice", choices: ["monthly", "quarterly"] };

let bad = 0;
function eq(label: string, got: unknown, want: unknown) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  const ok = g === w;
  if (!ok) bad++;
  console.log(`${ok ? "ok  " : "FAIL"}  ${label}\n        got=${g}\n        want=${w}`);
}

const BEFORE = "Commission is payable at {{commission_pct}}.";

console.log("── the case that was reported ──");
{
  const after = "Commission is payable at 15%.";
  const edits = chipEdits(BEFORE, after);
  eq("the slot is read", edits, [{ token: "commission_pct", text: "15%" }]);
  const move = readTermMove(edits[0], PCT);
  eq("the term moves to 15", move,
     { token: "commission_pct", value: "15", matched: "15%" });
  eq("and the chip goes back", restoreChips(BEFORE, after, [move!]), BEFORE);
}

console.log("── the prose typed around it survives ──");
{
  const after = "Commission is payable at 15% of gross premium.";
  const move = readTermMove(chipEdits(BEFORE, after)[0], PCT)!;
  eq("value", move.value, "15");
  eq("body", restoreChips(BEFORE, after, [move]),
     "Commission is payable at {{commission_pct}} of gross premium.");
}

console.log("── nothing is claimed where nothing is clear ──");
{
  const two = "Commission is payable at between 10% and 15%.";
  eq("two numbers is not an answer",
     readTermMove(chipEdits(BEFORE, two)[0], PCT), null);
  const words = "Commission is payable at the rate agreed by the parties.";
  eq("prose is not a value",
     readTermMove(chipEdits(BEFORE, words)[0], PCT), null);
  const gone = "Commission is payable at .";
  eq("a chip deleted outright",
     readTermMove(chipEdits(BEFORE, gone)[0], PCT), null);
  const rewritten = "The parties have agreed commission of 15 per cent.";
  eq("a sentence rewritten past recognition", chipEdits(BEFORE, rewritten), []);
}

console.log("── untouched chips are never reported ──");
{
  eq("no edit at all", chipEdits(BEFORE, BEFORE), []);
  const moved = "Commission is payable at {{commission_pct}} of premium.";
  eq("text added beside the chip", chipEdits(BEFORE, moved), []);
  const two = "Currency {{currency}}, commission {{commission_pct}}.";
  eq("one of two chips edited",
     chipEdits(two, "Currency USD, commission {{commission_pct}}."),
     [{ token: "currency", text: "USD" }]);
}

console.log("── the other kinds ──");
{
  const before = "The sum insured shall not exceed {{max_sum_insured}}.";
  const after = "The sum insured shall not exceed USD 1,250,000.";
  const move = readTermMove(chipEdits(before, after)[0], MONEY)!;
  eq("money loses its separators", move.value, "1250000");
  eq("but the currency word stays in the sentence",
     restoreChips(before, after, [move]),
     "The sum insured shall not exceed USD {{max_sum_insured}}.");

  const terr = "Business may be written in {{territory}}.";
  eq("free text is taken as typed",
     readTermMove(chipEdits(terr, "Business may be written in all US states.")[0],
                  TEXT)?.value,
     "all US states");

  const set = "Accounts are settled {{settlement_frequency}}.";
  eq("a choice must be one of the choices",
     readTermMove(chipEdits(set, "Accounts are settled quarterly.")[0], CHOICE)?.value,
     "quarterly");
  eq("and anything else is left alone",
     readTermMove(chipEdits(set, "Accounts are settled when convenient.")[0], CHOICE),
     null);
}

console.log("── a chip added from the toolbar is not an edit ──");
{
  const after = "Commission is payable at {{commission_pct}} in {{currency}}.";
  eq("the new chip is not mistaken for typing",
     chipEdits(BEFORE, after).filter(e => readTermMove(e, PCT)), []);
}

console.log(bad === 0 ? "\nALL SCENARIOS PASS" : `\n${bad} FAILED`);
process.exit(bad === 0 ? 0 : 1);
