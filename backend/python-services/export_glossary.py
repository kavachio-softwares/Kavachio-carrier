"""
export_glossary.py
──────────────────
Task 45.2 — plain-English meanings for every field and limit in the report
downloads, so a reader who has never seen the screen can still follow the file.

One place holds the words; each download adds them the same way:
  Excel   a sheet called "Column Guide"      (xlsx_sheet)
  PDF     a closing section "Column Guide"   (pdf_rows → the report's own table)

Reports covered (REPORTS): the Operational Report and the Executive Report (PDF and
Excel), Partner Performance, Policy Search and Audit Logs (Excel). A CSV has
no second sheet, so the Audit Logs CSV is left as it is.

A meaning may carry a limit that the report really uses; write it as {name} and
pass the value in `limits`, so the file states the number it was made with
(for example the fix-time target) instead of a fixed figure that could go stale.
Writers: short sentences, no jargon, no code words.
"""
from __future__ import annotations

from typing import Optional

TITLE = "Column Guide"

# (heading or None, [(word on the report, what it means)])
Section = tuple[Optional[str], list[tuple[str, str]]]

_FILE = ("File (bordereau)", "The monthly spreadsheet a broker sends the carrier, listing the policies sold and the "
         "premium collected. Each programme expects one file per broker per month.")
_CHECK = ("Check", "An automatic test Kavachio runs on every row of a file. For example: “Is the premium above zero?”, "
          "“Is the policy start date inside the contract dates?”. Most checks come from the contract's rules.")
_SEVERITY = ("Severity", "How serious a problem is. Critical: the data is wrong and must be fixed. Warning: something "
             "looks unusual and should be looked at. Info: a note only, nothing needs to be done.")
_OWNER = ("Who must act", "The person who has to fix it: the broker who sent the file, or the carrier if no broker "
          "sent it.")
_PERIOD = ("Period", "The day, week, month or dates this report covers. If the period has not ended yet, the numbers "
           "are as of the day the report was made and can still change.")

PERIODIC: list[Section] = [
    ("Terms used", [
        _PERIOD, _FILE, _CHECK,
        ("Problem / error", "Something a check found wrong in a file, such as a missing date or a wrong amount."),
        ("“vs” columns", "How this period compares with the period before, and with the same period last year. "
                         "Green means better, red means worse, grey means only more or less work, neither good nor bad."),
    ]),
    ("Timeliness: did files come on time?", [
        ("Files due", "How many files brokers had to send with a due date in this period. A programme with no broker "
                      "is not counted, because nobody owes it a file."),
        ("Arrived on time", "Of the files already due, how many came on or before their due date."),
        ("Arrived late", "Files that came, but after their due date."),
        ("Overdue", "The due date has passed, no file has come yet, and the broker can still send it."),
        ("Never arrived", "The next period has started and this period's file still never came."),
        ("Average days late", "For the files that came late: how many days late they were, on average."),
        ("Late-file cases still open", "Late files that are still being followed up. They can be Overdue (past due), "
                                       "Delinquent (still missing many days after the due date), In dispute (the broker "
                                       "disagrees) or Extended (the broker was given more time)."),
    ]),
    ("Volumes: how much came in?", [
        ("Files handled", "How many files were checked in the period. A file sent again or checked twice still counts "
                          "once."),
        ("Rows handled", "How many lines of data were checked. Empty lines, heading lines and total lines are not "
                         "counted."),
        ("New / Renewed / Cancelled", "How many policies were new, renewed or cancelled, read from the file's "
                                      "“Transaction Type” column. If a file has no policy numbers, lines are counted "
                                      "instead and the label says “(rows)”."),
    ]),
    ("Data quality: how good was the data?", [
        ("Errors found", "How many problems the checks found, counted on each file as it first arrived."),
        ("Critical errors", "Of those, how many were the most serious kind."),
        ("Rows with errors", "The percentage of lines that had at least one problem."),
        ("Files clean first time", "Files with no problems at all when they first arrived, so nobody had to fix or "
                                   "resend anything."),
        ("Most common problem", "The check that failed most often in the period."),
    ]),
    ("Fixes: how fast were problems fixed?", [
        ("Problems fixed", "How many problems were fixed in the period, whenever they were first found."),
        ("Average fix time", "On average, how long it took from finding a problem to fixing it."),
        ("Fixed within target", "How many problems were fixed within the target time. The target for this report is "
                                "{target}. It can be changed on the screen, and every change is recorded in Audit "
                                "Logs."),
        ("Waiting to be fixed", "Problems found but not yet fixed at the end of the period (or today, if the period "
                                "has not ended)."),
        ("Waiting longer than target", "Of those, how many have waited longer than the target time. These are the "
                                       "ones to chase first."),
    ]),
    ("By broker and programme", [
        ("Broker / Programme", "Each line is one broker on one programme."),
        ("Files due so far", "Files from that broker whose due date has already passed."),
        ("Arrived on time / On time %", "How many of those came on or before the due date, as a number and a "
                                        "percentage."),
        ("Files checked", "How many files from that broker were checked in the period."),
        ("Clean first time / %", "How many of those files had no problems when they arrived, as a number and a "
                                 "percentage."),
        ("Errors", "How many problems were found in that broker's files."),
        ("Average fix time (days)", "How many days that broker's problems took to fix, on average."),
        ("Waiting longer than target", "That broker's problems that are still not fixed after the target time."),
    ]),
    ("Checks that failed most", [
        ("#", "Position in the list. Number 1 failed most often."),
        _CHECK, _SEVERITY,
        ("Files", "How many files failed this check at least once."),
        ("Errors", "How many times this check failed in total, across all lines and files."),
    ]),
    ("Errors still waiting", [
        ("Waiting for", "How long problems have been waiting to be fixed, grouped into ranges: {age_bands}."),
        ("Errors", "How many problems fall into that range."),
        ("Past the target", "“Yes” if problems in that range have already waited longer than the {target} target."),
        _OWNER,
        ("Waiting / Longer than target", "How many of that person's problems are waiting, and how many of those are "
                                         "past the target time."),
        ("Longest waiting (days)", "How many days that person's oldest unfixed problem has been waiting."),
    ]),
    ("Waiting list", [
        ("Waiting for (days)", "How many days this problem has been waiting to be fixed."),
        ("Problem", "The check that found the problem."),
        ("Longer than target", "How many of these problems have waited longer than the target time."),
        ("File / Period", "The file the problem is in, and the month that file covers."),
    ]),
]

EXECUTIVE: list[Section] = [
    ("Terms used", [
        _PERIOD, _FILE, _CHECK,
        ("Programme", "A line of insurance business the carrier runs with one or more brokers, for example "
                      "“Commercial Property UK”."),
        ("Numbers in brackets", "How the figure changed compared with the period before. “Down from …” means a "
                                "percentage got worse."),
    ]),
    ("Headline", [
        ("Gross premium", "The total amount customers paid for their policies, as reported in the files, before "
                          "the broker's commission is taken off."),
        ("Commission", "The share of the premium that the broker keeps as payment for selling the policies."),
        ("Files processed", "How many files came in and were checked in the period."),
        ("On time", "The percentage of files that came on or before their due date."),
        ("Clean first time", "The percentage of files that had no problems at all when they first arrived."),
        ("Open problems", "Problems that are still waiting to be fixed. “Overdue” means they are past the date they "
                          "should have been fixed by."),
        ("Escalations", "Open problems that need a manager to step in: a file still missing long after its due "
                        "date, a disagreement waiting for the carrier's decision, or anything left open past its due "
                        "date."),
    ]),
    ("Files by status", [
        ("Clean", "Files that passed every check when they arrived."),
        ("With errors", "Files where at least one check found a problem."),
        ("Not checked", "Files that could not be checked, usually because the checks for that contract were not set "
                        "up yet."),
    ]),
    ("Top programmes and brokers by premium", [
        ("Programme / Broker", "The top {top} programmes, and the top {top} brokers, that brought in the most premium."),
        ("Gross premium", "Their total premium before commission is taken off."),
    ]),
    ("Programmes", [
        ("Active", "Programmes that are running and selling new policies."),
        ("Run-off", "Programmes that have stopped selling new policies but still look after the ones already sold."),
        ("Not live yet", "Programmes still being set up, not selling yet."),
        ("New / Went live / Went into run-off", "Programmes created, started selling, or stopped selling that day."),
    ]),
    ("Errors by hour, day, week or month", [
        ("Hour / Day / Week / Month", "The period is split into these steps so you can see the trend."),
        ("Files", "How many files were checked in that step."),
        ("Errors", "How many problems were found in those files."),
        ("Clean first time", "The percentage of those files that had no problems when they arrived."),
    ]),
    ("Most common errors", [
        _CHECK,
        ("Files", "How many files failed this check at least once."),
        ("Errors", "How many times this check failed in total."),
        _SEVERITY,
    ]),
    ("Broker and programme scorecards", [
        ("Broker / Programme", "Each line is one broker (or one programme)."),
        ("Premium", "The premium it brought in during the period."),
        ("Files", "How many of its files were checked."),
        ("On time", "The percentage of its files that came on or before the due date. “Down from …” shows it got "
                    "worse than the period before."),
        ("Clean first time", "The percentage of its files that had no problems when they arrived."),
        ("Late or missing", "How many of its files came late or have not come at all."),
        ("“n of N” in the title", "Only the top {scorecard_shown} are listed. The title tells you how many there "
                                  "are in total."),
    ]),
    ("Late submissions", [
        ("Broker / Programme / Month", "Who should have sent the file, for which programme, and which month it "
                                       "covers."),
        ("Due", "The date the file should have arrived."),
        ("Arrived", "The date it actually arrived. If it has not arrived, its status is shown instead."),
        ("Days late", "How many days after the due date it arrived."),
        ("How many are shown", "Only the {late_shown} most recent are listed. The title counts all of them."),
    ]),
    ("Contracts sending files", [
        ("Contract with no files", "A signed contract that sent no file in the period. Worth asking the broker why."),
        ("Broker / Programme", "Who the contract is with, and which programme it belongs to."),
    ]),
    ("Premium cap used", [
        ("Contract / Broker", "The contract and the broker it is with."),
        ("Premium cap", "The most premium the contract allows the broker to sell. Above this, the carrier has not "
                        "agreed to take on more risk."),
        ("Written so far", "How much premium has been sold under the contract so far."),
        ("Used", "How much of the cap has been used, as a percentage."),
        ("Near cap / Over cap", "“Near cap” means {near_cap}% or more is used. “Over cap” means more than 100% "
                                "is used, which breaks the contract's limit."),
    ]),
    ("Needs attention", [
        ("Problem", "What went wrong, and which file or case it belongs to."),
        _OWNER,
        ("Open for", "How many days it has been open."),
        ("Due", "The date it should have been sorted out by."),
        ("Escalated because", "Why it needs a manager: Disputed (someone disagrees), Delinquent (a file is still "
                              "missing long after its due date), or past its due date."),
        ("How many are shown", "At most {escalations_shown} are listed. The title counts all of them."),
    ]),
    ("Reinsurance statements due", [
        ("Treaty", "The agreement under which the carrier passes part of its risk, and premium, to a reinsurer."),
        ("Period", "The months the statement covers."),
        ("Due", "The date the statement had to be sent to the reinsurer."),
        ("Status", "Sent on time, Sent late, or Not sent yet."),
    ]),
]

PARTNER: list[Section] = [
    ("Terms used", [
        _FILE,
        ("Blank percentage", "A blank cell means there was nothing to measure, for example no files were due."),
    ]),
    ("Columns", [
        ("Rank", "The broker's position among all brokers in the period, 1 being the best. Ranked first by "
                 "“On time and passed checks %”, then by “On time %”, then by number of files. “Not ranked” means "
                 "none of its files could be judged yet."),
        ("Broker / Programme / Contract", "Each line is one broker, on one programme, under one contract."),
        ("Files due", "How many files the broker had to send in the period."),
        ("Files received", "How many of those have arrived."),
        ("On time", "How many arrived on or before their due date."),
        ("On time %", "On time as a percentage of files due."),
        ("Late", "How many arrived after their due date."),
        ("Missing", "How many were due but have not arrived."),
        ("Passed checks first time %", "Of the files that were checked, the percentage that had no problems when "
                                       "they first arrived."),
        ("On time and passed checks", "How many files were both on time and had no problems. This is the best "
                                      "result a broker can get."),
        ("Of files with a result", "How many files could be judged. Files that arrived on time but have not been "
                                   "checked yet are left out, so they do not count against the broker."),
        ("On time and passed checks %", "“On time and passed checks” as a percentage of “Of files with a result”. "
                                        "This is the main score used for the rank."),
        ("Not checked", "Files that arrived on time but have not been checked yet. They are not counted as good or "
                        "bad."),
        ("Files still with problems", "Files whose latest version still has problems waiting to be fixed."),
        ("Problems still open", "How many problems are still waiting to be fixed in those files."),
        ("Problems found", "How many problems were found when the files first arrived."),
        ("Problems put right", "How many of those problems have been fixed."),
        ("Put right %", "Problems put right as a percentage of problems found."),
        ("How it came in", "How the broker sent the file: {channels}. “{not_recorded}” means the way was not "
                           "saved; “{not_received}” means no file came."),
    ]),
]

POLICIES: list[Section] = [
    ("Columns", [
        ("Policy number", "The policy's reference number, as the broker wrote it in the file."),
        ("Insured", "The person or company the policy protects."),
        ("Programme", "The line of business the policy was sold under."),
        ("Broker / Carrier", "The carrier sees the broker who sold the policy. The broker sees the carrier who "
                             "insures it."),
        ("State", "The state or region written in the file, for example TX for Texas."),
        ("Cover start / Cover end", "The date the protection starts, and the date it ends."),
        ("Latest transaction", "The last thing that happened to the policy, for example New, Renewal, Change or "
                               "Cancellation."),
        ("Premium", "The amount the customer pays for the policy, from its latest line in the files."),
        ("Currency", "The currency of the premium, for example USD or GBP."),
        ("Latest bordereau", "The most recent month in which this policy appeared in a broker's file."),
        ("First bordereau", "The first month in which this policy appeared in a broker's file."),
        ("Bordereaux", "How many monthly files this policy has appeared in."),
        ("Rows", "How many lines this policy has across all those files. One policy can have several lines, for "
                 "example when it is changed or renewed."),
    ]),
]

AUDIT: list[Section] = [
    ("Columns", [
        ("Timestamp (UTC)", "The date and time it happened, in UTC (Coordinated Universal Time), which can differ "
                            "from your local time."),
        ("Actor", "Who did it. “Kavachio · Automatic” means the system did it on its own, with no person involved."),
        ("Role", "What kind of user they are: Kavachio (platform team), Carrier (the insurer) or Broker."),
        ("Organisation", "The company the person works for."),
        ("Carrier", "The insurer the action relates to."),
        ("Action", "What was done, in plain words, for example “Uploaded a file” or “Signed a contract”."),
        ("What exactly", "The details of the change, for example the old value and the new value."),
        ("Bulk update", "Filled in when many items were changed at once and are shown as one line."),
        ("Target type", "The kind of thing that was changed, for example a file, contract, programme or user."),
        ("Target / file", "The name of the thing that was changed."),
        ("Status", "Whether the action worked or failed."),
        ("Log", "Which group the line belongs to: Activity (work done), Sign-in (logging in and out), Access "
                "(files downloaded) or Exception (decisions on data problems)."),
        ("IP address", "The internet address of the computer it was done from. Useful for security checks."),
        ("Event name", "The system's short code for the action. Only needed when searching or asking support."),
    ]),
]

# What each way in means, keyed by the code partner_performance.CHANNEL_LABEL
# names; the list itself (and its words) always comes from there.
_CHANNEL_HELP = {"upload": "uploaded on the website", "sftp": "secure file transfer",
                 "api": "sent automatically by the broker's system",
                 "secure_link": "a correction sent through a link",
                 "fix_validate": "corrected and re-checked inside Kavachio",
                 "cloud_folder": "picked up from a shared cloud folder"}


def _from_code() -> dict:
    """Every number and list the meanings quote, read from the code that uses
    it, so a change there changes the Column Guide too (no copies)."""
    import executive_report as er
    import fix_performance as fp
    import partner_performance as pp
    ways = [f"{label} ({_CHANNEL_HELP[k]})" if k in _CHANNEL_HELP else label
            for k, label in pp.CHANNEL_LABEL.items() if k not in (pp.NOT_RECORDED, pp.NOT_RECEIVED)]
    return {"top": er.TOP, "near_cap": f"{er.NEAR_CAP_PCT:g}", "late_shown": er.LATE_SHOWN,
            "scorecard_shown": er.SCORECARD_SHOWN, "escalations_shown": er.ESCALATIONS_SHOWN,
            "age_bands": ", ".join(words for _k, words, _lo, _hi in fp.AGE_BANDS),
            "channels": ", ".join(ways[:-1]) + " or " + ways[-1] if len(ways) > 1 else "".join(ways),
            "not_recorded": pp.CHANNEL_LABEL[pp.NOT_RECORDED],
            "not_received": pp.CHANNEL_LABEL[pp.NOT_RECEIVED]}


REPORTS = {"periodic": PERIODIC, "executive": EXECUTIVE, "partner": PARTNER,
           "policies": POLICIES, "audit": AUDIT}


def rows(report: str, **limits) -> list[tuple[Optional[str], str, str]]:
    """Flat (section, word, meaning), limits filled in. A {name} with no
    value passed stays as written rather than failing the export."""
    class _Keep(dict):
        def __missing__(self, key):
            return "{" + key + "}"
    limits = {**_from_code(), **limits}
    out = []
    for heading, items in REPORTS[report]:
        for word, meaning in items:
            out.append((heading, word, meaning.format_map(_Keep(limits))))
    return out


def pdf_rows(report: str, **limits) -> list[list[str]]:
    """Rows for a two-column table: [word, meaning], header first. A section
    heading is a row of its own with the meaning left empty."""
    out = [["Column", "Meaning"]]
    last = object()
    for heading, word, meaning in rows(report, **limits):
        if heading != last:
            out.append([heading or "", ""])
            last = heading
        out.append([word, meaning])
    return out


def xlsx_sheet(wb, report: str, **limits):
    """Add the "Column Guide" sheet to an openpyxl workbook."""
    from openpyxl.styles import Alignment, Font, PatternFill
    sh = wb.create_sheet(TITLE)
    bold, grey = Font(bold=True), PatternFill("solid", fgColor="F3F4F7")
    sh.append(["Column", "Meaning"])
    for c in sh[1]:
        c.font, c.fill = bold, grey
    last = object()
    for heading, word, meaning in rows(report, **limits):
        if heading != last:
            sh.append([heading or ""])
            sh.cell(row=sh.max_row, column=1).font = bold
            sh.cell(row=sh.max_row, column=1).fill = grey
            sh.cell(row=sh.max_row, column=2).fill = grey
            last = heading
        sh.append([word, meaning])
    sh.column_dimensions["A"].width = 34
    sh.column_dimensions["B"].width = 100
    for row in sh.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    sh.freeze_panes = "A2"
    return sh
