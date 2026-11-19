"""
export_glossary.py
──────────────────
Task 45.2 — the Column Guide: a plain-English meaning for every column in a
report download, so a reader who has never seen the screen can follow the file.

The guide is built FROM THE FILE, after the file is made:
  Excel   read_xlsx(wb)       reads each sheet's heading rows   → xlsx_sheet adds the "Column Guide" sheet
  PDF     read_pdf(story)     reads each table's heading row    → pdf_rows gives the closing table
so it lists only the columns that file really has: a section left out, or a
heading that changes with the period ("Fixed within target (5 days)", "New
(rows)", "vs the week before", "October 2026"), is followed automatically.

Meanings are kept ONCE per idea (TERMS), shared by every report, and found by
a loose match (match): case, quotes, brackets, "%" and a trailing "at month
end" / "so far" do not matter. A heading with no meaning yet still gets a
plain one built from its name and the kind of values under it (generic), and
the tests list every such heading so it can be given a proper meaning.

Nothing is stored in the database. Reports covered: the Operational Report and
the Executive Report (PDF and Excel), Partner Performance, Policy Search and
Audit Logs (Excel). A CSV has no second sheet, so the Audit Logs CSV is left as
it is.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any, Iterable, Optional

TITLE = "Column Guide"
HEADER = ["Column", "Meaning", "Where it appears"]
INTRO = "This guide explains each column in this file."

# ── the shared meanings: (headings that mean the same thing, meaning) ────────
# A {name} is filled from limits() — a number the code really uses — or from
# what the caller passes (the fix-time target of that report).
TERMS: list[tuple[tuple[str, ...], str]] = [
    # who and what
    (("Broker",), "The company that sells the policies and sends the files to the carrier."),
    (("Carrier",), "The insurance company that carries the risk and receives the files."),
    (("Programme",), "The line of insurance business the line belongs to, for example “Commercial Property UK”."),
    (("Contract",), "The signed agreement between the carrier and the broker. Files are checked against its rules."),
    (("Treaty",), "The agreement under which the carrier passes part of its risk, and premium, to a reinsurer."),
    (("Who must act", "Owner"), "The person who has to fix it: the broker who sent the file, or the carrier if no "
                                "broker sent it."),
    (("Rank",), "The broker's position among all brokers in the period, 1 being the best. Ranked first by "
                "“On time and passed checks %”, then by “On time %”, then by number of files. “Not ranked” means "
                "none of its files could be judged yet."),
    (("#",), "Position in the list. Number 1 is the highest."),
    # when
    (("Period",), "The month (or other period) the file or statement covers."),
    (("Month",), "The month the line is about."),
    (("Week",), "The week the line is about."),
    (("Day",), "The day the line is about."),
    (("Hour",), "The hour of the day the line is about."),
    (("Due",), "The date by which it had to arrive or be done."),
    (("Arrived",), "The date the file actually arrived. If it has not arrived, its status is shown instead."),
    (("Days late",), "How many days after the due date the file arrived."),
    (("Open for",), "How many days it has been open."),
    (("Now",), "Where it stands today: still open, or how it ended."),
    # timeliness
    (("Files due", "Files due so far"), "How many files brokers had to send whose due date has passed or falls in "
                                        "the period. A programme with no broker is not counted."),
    (("Files received",), "How many of the files due have arrived."),
    (("On time", "Arrived on time", "Files on time"), "Files that arrived on or before their due date."),
    (("On time %",), "Files that arrived on or before their due date, as a percentage of the files due."),
    (("Late", "Arrived late"), "Files that arrived, but after their due date."),
    (("Missing",), "Files that were due but have not arrived."),
    (("Late or missing",), "Files that arrived late or have not arrived at all."),
    (("Overdue",), "The due date has passed, no file has come yet, and the broker can still send it."),
    (("Never arrived",), "The next period has started and this period's file still never came."),
    (("Average days late",), "For the files that came late: how many days late they were, on average."),
    (("Late-file cases still open",), "Late files still being followed up. They can be Overdue (past due), "
                                      "Delinquent (still missing many days after the due date), In dispute (the "
                                      "broker disagrees) or Extended (the broker was given more time)."),
    # volumes
    (("Files", "Files handled", "Files processed", "Files checked"),
     "How many files were checked. A file sent again or checked twice still counts once."),
    (("Rows handled",), "How many lines of data were checked. Empty, heading and total lines are not counted."),
    (("New", "New policies"), "Policies sold for the first time, read from the file's “Transaction Type” column. "
                              "“(rows)” means lines were counted because the file had no policy numbers."),
    (("Renewed", "Renewed policies"), "Policies renewed for another term, read from the “Transaction Type” column."),
    (("Cancelled", "Cancelled policies"), "Policies cancelled, read from the “Transaction Type” column."),
    # quality
    (("Errors", "Errors found"), "How many problems the checks found, counted on each file as it first arrived."),
    (("Critical errors",), "Of those, how many were the most serious kind, which must be fixed."),
    (("Rows with errors",), "The percentage of lines that had at least one problem."),
    (("Clean first time", "Files clean first time", "Passed checks first time"),
     "Files with no problems at all when they first arrived, so nobody had to fix or resend anything."),
    (("Clean first time %", "Passed checks first time %"),
     "Of the files checked, the percentage that had no problems when they first arrived."),
    (("On time and passed checks",), "Files that were both on time and had no problems. The best result a broker "
                                     "can get."),
    (("Of files with a result",), "How many files could be judged. Files that arrived on time but have not been "
                                  "checked yet are left out, so they do not count against the broker."),
    (("On time and passed checks %",), "“On time and passed checks” as a percentage of “Of files with a result”. "
                                       "This is the main score used for the rank."),
    (("Not checked",), "Files that could not be checked yet, usually because the checks for that contract were not "
                       "set up. They are not counted as good or bad."),
    (("Clean",), "Files that passed every check when they arrived."),
    (("With errors",), "Files where at least one check found a problem."),
    (("Most common problem",), "The check that failed most often in the period."),
    (("Check",), "The automatic test that found the problem, for example “Premium is a number” or "
                 "“Policy start date is inside the contract dates”."),
    (("Problem",), "What went wrong, and which file or case it belongs to."),
    (("Severity",), "How serious a problem is. Critical: the data is wrong and must be fixed. Warning: something "
                    "looks unusual and should be looked at. Info: a note only, nothing needs to be done."),
    # fixes
    (("Problems fixed", "Problems put right"), "How many problems have been fixed."),
    (("Put right %",), "Problems fixed as a percentage of problems found."),
    (("Problems found",), "How many problems were found when the files first arrived."),
    (("Problems still open", "Open problems", "Waiting", "Waiting to be fixed"),
     "Problems found but not fixed yet (at the end of the period, or today if the period has not ended)."),
    (("New problems",), "Problems found that day."),
    (("Files still with problems",), "Files whose latest version still has problems waiting to be fixed."),
    (("Average fix time",), "On average, how long it took from finding a problem to fixing it."),
    (("Fixed within target",), "How many problems were fixed within the target time ({target})."),
    (("Waiting longer than target", "Longer than target", "Past the target"),
     "Problems that have waited longer than the target time ({target}) to be fixed. These are the ones to chase "
     "first."),
    (("Waiting for",), "How long the problems have been waiting to be fixed: {age_bands}."),
    (("Longest waiting",), "How many days the oldest unfixed problem has been waiting."),
    (("Escalations", "New escalations"), "Open problems that need a manager to step in: a file still missing long "
                                         "after its due date, a disagreement waiting for the carrier's decision, "
                                         "or anything left open past its due date."),
    (("Escalated because",), "Why it needs a manager: Disputed (someone disagrees), Delinquent (a file still "
                             "missing long after its due date), or past its due date."),
    (("File",), "The name of the file the problem is in."),
    # money
    (("Gross premium",), "The total amount customers paid for their policies, as reported in the files, before the "
                         "broker's commission is taken off."),
    (("Commission",), "The share of the premium that the broker keeps as payment for selling the policies."),
    (("Premium",), "The amount customers pay for their policies."),
    (("Currency",), "The currency of the amount, for example USD or GBP."),
    (("Premium cap",), "The most premium the contract allows the broker to sell. Above this, the carrier has not "
                       "agreed to take on more risk."),
    (("Written so far",), "How much premium has been sold under the contract so far."),
    (("Used",), "How much of the premium cap has been used, as a percentage. “Near cap” from {near_cap}%, "
                "“Over cap” above 100%."),
    # programmes
    (("Active",), "Programmes that are running and selling new policies."),
    (("Run-off", "Went into run-off"), "Programmes that have stopped selling new policies but still look after the "
                                       "ones already sold."),
    (("Not live yet",), "Programmes still being set up, not selling yet."),
    (("Went live",), "Programmes that started selling that day."),
    (("Total", "Programmes in all"), "All of them added together."),
    (("Contract with no files",), "A signed contract that sent no file in the period. Worth asking the broker why."),
    # status
    (("Status",), "Where it stands. For a reinsurance statement: Sent on time, Sent late or Not sent. For an audit "
                  "line: whether the action worked."),
    (("How it came in",), "How the broker sent the file: {channels}. “{not_recorded}” means the way was not saved; "
                          "“{not_received}” means no file came."),
    # policies
    (("Policy number",), "The policy's reference number, as the broker wrote it in the file."),
    (("Insured",), "The person or company the policy protects."),
    (("State",), "The state or region written in the file, for example TX for Texas."),
    (("Cover start",), "The date the protection starts."),
    (("Cover end",), "The date the protection ends."),
    (("Latest transaction",), "The last thing that happened to the policy, for example New, Renewal, Change or "
                              "Cancellation."),
    (("Latest bordereau",), "The most recent month in which this policy appeared in a broker's file."),
    (("First bordereau",), "The first month in which this policy appeared in a broker's file."),
    (("Bordereaux",), "How many monthly files this policy has appeared in."),
    (("Rows",), "How many lines it has across those files. One policy can have several lines, for example when "
                "it is changed or renewed."),
    # audit
    (("Timestamp",), "The date and time it happened, in UTC (Coordinated Universal Time), which can differ from "
                     "your local time."),
    (("Actor",), "Who did it. “Kavachio · Automatic” means the system did it on its own, with no person involved."),
    (("Role",), "What kind of user they are: Kavachio (platform team), Carrier (the insurer) or Broker."),
    (("Organisation",), "The company the person works for."),
    (("Action",), "What was done, in plain words, for example “Uploaded a file” or “Signed a contract”."),
    (("What exactly",), "The details of the change, for example the old value and the new value."),
    (("Bulk update",), "Filled in when many items were changed at once and are shown as one line."),
    (("Target type",), "The kind of thing that was changed, for example a file, contract, programme or user."),
    (("Target", "Target / file"), "The name of the thing that was changed."),
    (("Log",), "Which group the line belongs to: Activity (work done), Sign-in (logging in and out), Access (files "
               "downloaded) or Exception (decisions on data problems)."),
    (("IP address",), "The internet address of the computer it was done from. Useful for security checks."),
    (("Event name",), "The system's short code for the action. Only needed when searching or asking support."),
    # comparisons
    (("vs",), "How this period compares with the period named. Green means better, red means worse, grey means "
              "only more or less work, neither good nor bad."),
]

PERIOD_MEANING = "The figures for each period named, side by side so you can compare them."

# A table whose first heading is one of these lists its fields DOWN the first
# column (Key numbers, Headline, Programmes, Files by status), not across.
_ROW_LABEL_TABLES = {"measure", "headline", "programmes", "programmes that day", "status"}
# Headings that only say "the value" — not a column worth explaining.
_SKIP = {"", "this period", "count", "this day", "this week", "this month"}

# What each way in means, keyed by the code partner_performance.CHANNEL_LABEL
# names; the list itself (and its words) always comes from there.
_CHANNEL_HELP = {"upload": "uploaded on the website", "sftp": "secure file transfer",
                 "api": "sent automatically by the broker's system",
                 "secure_link": "a correction sent through a link",
                 "fix_validate": "corrected and re-checked inside Kavachio",
                 "cloud_folder": "picked up from a shared cloud folder"}


def limits() -> dict:
    """Every number and list the meanings quote, read from the code that uses
    it, so a change there changes the Column Guide too (no copies)."""
    import executive_report as er
    import fix_performance as fp
    import partner_performance as pp
    ways = [f"{label} ({_CHANNEL_HELP[k]})" if k in _CHANNEL_HELP else label
            for k, label in pp.CHANNEL_LABEL.items() if k not in (pp.NOT_RECORDED, pp.NOT_RECEIVED)]
    return {"target": "the target set on the Exception Resolution screen",
            "near_cap": f"{er.NEAR_CAP_PCT:g}",
            "age_bands": ", ".join(words for _k, words, _lo, _hi in fp.AGE_BANDS),
            "channels": ", ".join(ways[:-1]) + " or " + ways[-1] if len(ways) > 1 else "".join(ways),
            "not_recorded": pp.CHANNEL_LABEL[pp.NOT_RECORDED],
            "not_received": pp.CHANNEL_LABEL[pp.NOT_RECEIVED]}


# ── matching a heading to its meaning ───────────────────────────────────────
_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_TAIL = re.compile(r"\s+(at|so far|in this period|that day|in the period)\b.*$")


def norm(heading: Any) -> str:
    """The heading reduced to its words: lower case, no quotes, no brackets
    ("(5 days)", "(rows)", "(UTC)"), "%" kept as a word, spaces tidied."""
    t = str(heading or "").strip().lower()
    if t == "#":
        return "#"
    t = re.sub(r"\([^)]*\)", " ", t).replace("%", " % ")
    t = re.sub(r"[“”\"‘’']", "", t)
    t = re.sub(r"[^a-z0-9%&\- ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


_INDEX: dict[str, tuple[str, str]] = {}
for _names, _meaning in TERMS:
    for _n in _names:
        _INDEX[norm(_n)] = (norm(_names[0]), _meaning)


def is_period(heading: Any) -> bool:
    """A column named after a period ("October 2026", "Week of 26 Oct 2026")."""
    return bool(_YEAR.search(str(heading or "")))


def match(heading: Any) -> Optional[tuple[str, str]]:
    """(key, meaning) for a heading, or None. Exact words first; then the
    heading without a trailing "at month end" / "so far" / "that day"; then the
    longest known heading it starts with ("vs last month" → "vs",
    "Contract with no files in this period" → "Contract with no files")."""
    n = norm(heading)
    for cand in (n, _TAIL.sub("", n)):
        if cand in _INDEX:
            return _INDEX[cand]
    starts = [k for k in _INDEX if n.startswith(k + " ")]
    return _INDEX[max(starts, key=len)] if starts else None


# ── a heading nobody has explained yet ──────────────────────────────────────
_PCT_TEXT = re.compile(r"^-?[\d.,]+\s*%")
_NUM_TEXT = re.compile(r"^-?[\d,]+(\.\d+)?$")
_DATE_TEXT = re.compile(r"^(\d{4}-\d{2}-\d{2}|\d{1,2} [A-Za-z]{3,9} \d{4})")
_MONEY_TEXT = re.compile(r"^([A-Z]{3}|[$£€₹])\s?-?[\d,]+")


def kind_of(heading: Any, cells: Iterable[tuple[Any, Optional[str]]]) -> str:
    """What a column holds — percentage, amount, date, number or text — from
    its values (and their Excel number format), else from its name."""
    seen = {"percentage": 0, "amount": 0, "date": 0, "number": 0, "text": 0}
    for v, fmt in cells:
        if v in (None, ""):
            continue
        s = v.strip() if isinstance(v, str) else None
        if (fmt and "%" in fmt) or (s is not None and _PCT_TEXT.match(s)):
            seen["percentage"] += 1
        elif isinstance(v, (dt.date, dt.datetime)) or (s is not None and _DATE_TEXT.match(s)):
            seen["date"] += 1
        elif s is not None and _MONEY_TEXT.match(s):
            seen["amount"] += 1
        elif (isinstance(v, (int, float)) and not isinstance(v, bool)) or (s is not None and _NUM_TEXT.match(s)):
            seen["number"] += 1
        else:
            seen["text"] += 1
    best = max(seen, key=seen.get)
    if seen[best]:
        return best
    n = norm(heading)
    if "%" in n:
        return "percentage"
    if re.search(r"\b(premium|amount|commission|cost|price|fee)\b", n):
        return "amount"
    if re.search(r"\b(date|start|end|due|day|when)\b", n):
        return "date"
    if re.search(r"\b(count|number|files|rows|errors|problems|days|how many)\b", n):
        return "number"
    return "text"


def generic(heading: Any, kind: str) -> str:
    """A plain meaning for a heading with no meaning in TERMS yet."""
    name = str(heading).strip()
    return {
        "percentage": f"“{name}” shown as a percentage: the share out of the total on that line.",
        "amount": f"The money amount for “{name}” on each line, in the currency shown.",
        "date": f"The date recorded as “{name}” for each line.",
        "number": f"How many, or how much, “{name}” for each line, as a number.",
        "text": f"The “{name}” recorded for each line, as written in the report.",
    }[kind]


# ── reading the file's own tables ───────────────────────────────────────────
# A table: {"where": sheet or section, "header": [str], "body": [[(value, format)]],
#           "labels": True when its fields run down the first column}


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):          # reportlab keeps a wrapped cell as a list
        return " ".join(_text(x) for x in v).strip()
    if hasattr(v, "getPlainText"):
        return v.getPlainText()
    return str(v)


def _bold(c) -> bool:
    return c.font is not None and bool(c.font.b)


def read_xlsx(wb, skip: Iterable[str] = ()) -> list[dict]:
    """Every table in a workbook: a heading row is a row of two or more filled
    cells, all bold; its table runs to the next empty row. A sheet with no
    heading row but bold labels in column A beside values (the Executive
    Report's Headline) is read as a list of labels."""
    skip = set(skip) | {TITLE}
    tables = []
    for ws in wb.worksheets:
        if ws.title in skip:
            continue
        found, cur, labels = [], None, []
        for row in ws.iter_rows():
            filled = [c for c in row if c.value not in (None, "")]
            if not filled:
                cur = None
                continue
            if len(filled) >= 2 and all(_bold(c) for c in filled):
                cur = {"where": ws.title, "header": [_text(c.value) for c in row], "body": [], "labels": False}
                found.append(cur)
                continue
            if cur is not None:
                cur["body"].append([(c.value, c.number_format) for c in row])
            if len(row) >= 2 and row[0].value not in (None, "") and _bold(row[0]) \
                    and row[1].value not in (None, ""):
                labels.append([(row[0].value, None), (row[1].value, row[1].number_format)])
        if not found and labels:
            found.append({"where": ws.title, "header": ["Headline", ""], "body": labels, "labels": True})
        tables.extend(found)
    return tables


def section_name(heading: str) -> str:
    """A section's name without the live counts its heading carries:
    "Needs attention — 1 escalation" → "Needs attention",
    "Errors still waiting, as at 4 Nov 2026" → "Errors still waiting"."""
    return re.split(r"\s+—\s+|,\s+as at\b", heading, maxsplit=1)[0].strip()


def read_pdf(story: list) -> list[dict]:
    """Every table in a reportlab story, each under the heading (an h2
    paragraph) above it; the first ones sit under "Top of the report"."""
    from reportlab.platypus import Paragraph, Table
    tables, where = [], ["Top of the report"]

    def walk(items):
        for f in items:
            if isinstance(f, Paragraph) and getattr(f.style, "name", "") == "h2":
                where[0] = section_name(f.getPlainText())
            elif isinstance(f, Table):
                cells = [[_text(v) for v in r] for r in f._cellvalues]
                if cells:
                    tables.append({"where": where[0], "header": cells[0],
                                   "body": [[(v, None) for v in r] for r in cells[1:]], "labels": False})
            elif hasattr(f, "_content"):
                walk(f._content)
    walk(story)
    return tables


# ── the guide ───────────────────────────────────────────────────────────────
def _columns(t: dict) -> list[tuple[str, list]]:
    """(heading, its cells) for every field a table shows."""
    header, body = t["header"], t["body"]
    out = []
    if t.get("labels") or norm(header[0] if header else "") in _ROW_LABEL_TABLES:
        for c, h in enumerate(header[1:], 1):
            out.append((h, [r[c] for r in body if c < len(r)]))
        for r in body:
            rest = [x for x in r[1:] if x[0] not in (None, "")]
            if r and r[0][0] not in (None, "") and rest:
                out.append((_text(r[0][0]), rest))
        return out
    for c, h in enumerate(header):
        out.append((h, [r[c] for r in body if c < len(r)]))
    return out


def build(tables: list[dict], **given) -> list[dict]:
    """One entry per idea, in the order the file shows it:
    {heading, meaning, where: [..], generic: bool}. Period columns share one
    entry; a heading with no meaning yet gets generic()."""
    lim = {**limits(), **given}

    class _Keep(dict):
        def __missing__(self, key):
            return "{" + key + "}"
    entries: dict[str, dict] = {}
    periods: dict = {"headings": [], "where": []}
    for t in tables:
        for h, cells in _columns(t):
            h = _text(h).strip()
            if norm(h) in _SKIP:
                continue
            if is_period(h):
                if h not in periods["headings"]:
                    periods["headings"].append(h)
                if t["where"] not in periods["where"]:
                    periods["where"].append(t["where"])
                continue
            hit = match(h)
            key = hit[0] if hit else "?" + norm(h)
            e = entries.get(key)
            if e is None:
                meaning = hit[1].format_map(_Keep(lim)) if hit else generic(h, kind_of(h, cells))
                e = entries[key] = {"heading": h, "meaning": meaning, "where": [], "generic": not hit}
            if t["where"] not in e["where"]:
                e["where"].append(t["where"])
    out = list(entries.values())
    if periods["headings"]:
        out.insert(0, {"heading": ", ".join(periods["headings"]), "meaning": PERIOD_MEANING,
                       "where": periods["where"], "generic": False})
    return out


def unexplained(tables: list[dict]) -> list[str]:
    """Headings that fell back to generic() — the ones still to be given a meaning."""
    return [e["heading"] for e in build(tables) if e["generic"]]


def pdf_rows(story: list, **given) -> list[list[str]]:
    """The Column Guide for a PDF, read from its own tables: [HEADER, *rows]."""
    return [HEADER] + [[e["heading"], e["meaning"], ", ".join(e["where"])]
                       for e in build(read_pdf(story), **given)]


def xlsx_sheet(wb, skip: Iterable[str] = (), **given):
    """Add the Column Guide sheet, read from the workbook's own sheets."""
    from openpyxl.styles import Alignment, Font, PatternFill
    entries = build(read_xlsx(wb, skip), **given)
    sh = wb.create_sheet(TITLE)
    sh.append([INTRO])
    sh.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(HEADER))
    sh.row_dimensions[1].height = 20
    sh.append(HEADER)
    for c in sh[2]:
        c.font, c.fill = Font(bold=True), PatternFill("solid", fgColor="F3F4F7")
    for e in entries:
        sh.append([e["heading"], e["meaning"], ", ".join(e["where"])])
    for col, width in zip("ABC", (34, 90, 34)):
        sh.column_dimensions[col].width = width
    for row in sh.iter_rows():
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    sh.freeze_panes = "A3"
    return sh
