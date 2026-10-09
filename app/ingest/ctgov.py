"""ClinicalTrials.gov: sponsor aliases, current trials, daily snapshots, and change detection.

Run:  python -m app.ingest.ctgov --tickers SMMT,RVMD     (or ALL)
Registry dates are sponsor-entered estimates and are often stale. Treat the
events created here as signals to investigate, not facts.
"""
import argparse
import csv
import logging
import os
import re
from datetime import date, datetime

from rapidfuzz import fuzz

from app.config import project_path, setup_logging
from app.db import create_tables, get_connection, run_query
from app.http_utils import get_json

log = logging.getLogger(__name__)

STUDIES_URL = "https://clinicaltrials.gov/api/v2/studies"
STUDY_FIELDS = ("NCTId,BriefTitle,OverallStatus,Phase,EnrollmentCount,PrimaryOutcomeMeasure,"
                "PrimaryCompletionDate,LastUpdatePostDate,LeadSponsorName,CollaboratorName")
KEEP_PHASES = ["PHASE1", "PHASE2", "PHASE3"]
FUZZY_THRESHOLD = 90
COMPANY_SUFFIXES = ["common stock", "ordinary shares", "inc", "corp", "corporation", "ltd",
                    "limited", "plc", "co", "company", "holdings", "therapeutics inc"]
BAD_STATUSES = ["TERMINATED", "SUSPENDED", "WITHDRAWN"]


# ----------------------------------------------------------- name matching

def normalize_name(name):
    """Lowercase, drop punctuation and company suffixes like 'Inc.' / 'Corp.'."""
    text = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    words = [w for w in text.split() if w not in COMPANY_SUFFIXES]
    return " ".join(words)


def clean_company_name(name):
    """Remove ' - Common Stock' style endings from Nasdaq Trader security names."""
    return name.split(" - ")[0].strip()


def match_sponsor(sponsor_name, company_name):
    """Return 'exact', 'fuzzy', or None for how a sponsor name matches a company."""
    a = normalize_name(sponsor_name)
    b = normalize_name(company_name)
    if a == b:
        return "exact"
    if fuzz.ratio(a, b) >= FUZZY_THRESHOLD:
        return "fuzzy"
    return None


def read_manual_aliases():
    """Read data/manual_aliases.csv (alias,ticker); these always win."""
    path = project_path("data/manual_aliases.csv")
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return [(row["alias"], row["ticker"]) for row in csv.DictReader(f)]


# ------------------------------------------------------------- API parsing

def parse_study(study, ticker):
    """Turn one API study record into a flat dict matching the trials table."""
    protocol = study.get("protocolSection", {})
    status = protocol.get("statusModule", {})
    outcomes = protocol.get("outcomesModule", {}).get("primaryOutcomes", [])
    return {
        "nct_id": protocol["identificationModule"]["nctId"],
        "ticker": ticker,
        "title": protocol["identificationModule"].get("briefTitle"),
        "phase": ",".join(protocol.get("designModule", {}).get("phases", [])),
        "status": status.get("overallStatus"),
        "enrollment": protocol.get("designModule", {}).get("enrollmentInfo", {}).get("count"),
        "primary_endpoint": "; ".join(o.get("measure", "") for o in outcomes),
        "primary_completion_date": status.get("primaryCompletionDateStruct", {}).get("date"),
        "last_update_posted": status.get("lastUpdatePostDateStruct", {}).get("date"),
    }


def search_studies(sponsor_name, max_pages=5):
    """Return raw study records whose sponsor/collaborator matches the name."""
    studies = []
    params = {"query.spons": sponsor_name, "pageSize": 100}
    for _ in range(max_pages):
        data = get_json(STUDIES_URL, params=params)
        if data is None:
            break
        studies.extend(data.get("studies", []))
        token = data.get("nextPageToken")
        if not token:
            break
        params["pageToken"] = token
    return studies


def get_sponsor_names(study):
    """Lead sponsor plus collaborator names of a raw study record."""
    module = study.get("protocolSection", {}).get("sponsorCollaboratorsModule", {})
    names = []
    if "leadSponsor" in module:
        names.append(module["leadSponsor"]["name"])
    for collaborator in module.get("collaborators", []):
        names.append(collaborator["name"])
    return names


# ----------------------------------------------------------------- aliases

def build_aliases(ticker, company_name):
    """Find sponsor names that belong to a company; returns (matched, unmatched) lists."""
    short_name = clean_company_name(company_name)
    seen = set()
    matched = []
    unmatched = []
    for study in search_studies(short_name, max_pages=2):
        for sponsor in get_sponsor_names(study):
            if sponsor in seen:
                continue
            seen.add(sponsor)
            method = match_sponsor(sponsor, short_name)
            if method:
                matched.append((sponsor, ticker, method))
            else:
                unmatched.append((sponsor, ticker))
    return matched, unmatched


def save_aliases(matched, review_rows):
    """Save matched aliases (manual ones win) and write unmatched ones for human review."""
    conn = get_connection()
    for alias, ticker, method in matched:
        conn.execute("INSERT OR IGNORE INTO sponsor_aliases (alias, ticker, match_method) "
                     "VALUES (?, ?, ?)", (alias, ticker, method))
    for alias, ticker in read_manual_aliases():
        conn.execute("INSERT OR REPLACE INTO sponsor_aliases (alias, ticker, match_method) "
                     "VALUES (?, ?, 'manual')", (alias, ticker))
    conn.commit()
    conn.close()
    with open(project_path("data/alias_review.csv"), "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["sponsor_name", "searched_for_ticker"])
        writer.writerows(review_rows)


# ------------------------------------------------------------------ trials

ACTIVE_STATUSES = ["RECRUITING", "ACTIVE_NOT_RECRUITING", "ENROLLING_BY_INVITATION",
                   "NOT_YET_RECRUITING"]


def is_worth_keeping(trial):
    """Keep Phase 1-3 trials that are active, or finished/stopped within the last year."""
    phases = trial["phase"].split(",")
    if not any(p in KEEP_PHASES for p in phases):
        return False
    if trial["status"] in ACTIVE_STATUSES:
        return True
    last = parse_loose_date(trial["last_update_posted"])
    # Old completed/terminated trials are history, not signals.
    return last is not None and (date.today() - last).days <= 365


def fetch_trials_for_ticker(ticker):
    """Fetch and save current trial state for all of a ticker's aliases; returns count."""
    aliases = run_query("SELECT alias FROM sponsor_aliases WHERE ticker = ?", (ticker,))
    conn = get_connection()
    now = datetime.now().isoformat(timespec="seconds")
    saved = 0
    seen_ncts = set()
    for row in aliases:
        for study in search_studies(row["alias"]):
            trial = parse_study(study, ticker)
            if trial["nct_id"] in seen_ncts or not is_worth_keeping(trial):
                continue
            seen_ncts.add(trial["nct_id"])
            conn.execute(
                "INSERT OR REPLACE INTO trials (nct_id, ticker, title, phase, status, enrollment, "
                "primary_endpoint, primary_completion_date, last_update_posted, last_fetched) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (trial["nct_id"], ticker, trial["title"], trial["phase"], trial["status"],
                 trial["enrollment"], trial["primary_endpoint"], trial["primary_completion_date"],
                 trial["last_update_posted"], now))
            saved += 1
    conn.commit()
    conn.close()
    return saved


# --------------------------------------------------------- snapshots + diff

def parse_loose_date(text):
    """Parse '2026-12' or '2026-12-31' into a date (month-only means the 1st); None if bad."""
    if not text:
        return None
    try:
        if len(text) == 7:
            text = text + "-01"
        return date.fromisoformat(text)
    except ValueError:
        return None


def diff_snapshots(old_row, new_row):
    """Compare two trial dicts; return a list of (description, materiality) changes.

    Keys used: status, enrollment, primary_endpoint, primary_completion_date, nct_id.
    """
    changes = []
    nct = new_row.get("nct_id", "trial")

    old_status, new_status = old_row.get("status"), new_row.get("status")
    if old_status != new_status:
        text = f"{nct}: status {old_status} -> {new_status}"
        if new_status in BAD_STATUSES:
            changes.append((text, 5))
        elif new_status == "COMPLETED":
            changes.append((text + " (data may be coming)", 4))  # topline results often follow
        else:
            changes.append((text, 2))

    old_date = old_row.get("primary_completion_date")
    new_date = new_row.get("primary_completion_date")
    if old_date != new_date:
        text = f"{nct}: primary completion date moved {old_date} -> {new_date}"
        old_parsed, new_parsed = parse_loose_date(old_date), parse_loose_date(new_date)
        if old_parsed and new_parsed and (new_parsed - old_parsed).days > 91:
            changes.append((text + " (delayed)", 4))
        elif old_parsed and new_parsed and new_parsed < old_parsed:
            changes.append((text + " (earlier)", 3))

    old_n, new_n = old_row.get("enrollment"), new_row.get("enrollment")
    if old_n and new_n and abs(new_n - old_n) / old_n > 0.15:
        changes.append((f"{nct}: enrollment changed {old_n} -> {new_n}", 3))

    if old_row.get("primary_endpoint") != new_row.get("primary_endpoint"):
        changes.append((f"{nct}: primary endpoint text changed", 4))
    return changes


def take_snapshots(today):
    """Write one snapshot row per current trial for the given date (safe to rerun)."""
    conn = get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO trial_snapshots (nct_id, snapshot_date, status, enrollment, "
        "primary_endpoint, primary_completion_date) "
        "SELECT nct_id, ?, status, enrollment, primary_endpoint, primary_completion_date FROM trials",
        (today,))
    conn.commit()
    conn.close()


def run_trial_diff(today):
    """Compare today's snapshots to each trial's previous one and save events; returns count."""
    earlier = run_query("SELECT COUNT(*) AS n FROM trial_snapshots WHERE snapshot_date < ?", (today,))
    first_run = earlier[0]["n"] == 0  # otherwise every trial would look "new" on day one
    trials = run_query("SELECT t.*, s.snapshot_date FROM trials t "
                       "JOIN trial_snapshots s ON s.nct_id = t.nct_id AND s.snapshot_date = ?", (today,))
    conn = get_connection()
    event_count = 0
    for trial in trials:
        new_row = dict(trial)
        old = conn.execute("SELECT * FROM trial_snapshots WHERE nct_id = ? AND snapshot_date < ? "
                           "ORDER BY snapshot_date DESC LIMIT 1", (trial["nct_id"], today)).fetchone()
        if old is None:
            changes = [] if first_run else [(f"{trial['nct_id']}: new trial appeared", 3)]
        else:
            changes = diff_snapshots(dict(old), new_row)
        if changes:
            description = "; ".join(text for text, _ in changes)
            materiality = max(level for _, level in changes)
            cursor = conn.execute(
                "INSERT OR IGNORE INTO events (ticker, event_date, event_type, description, "
                "materiality, source_ref, created_at) VALUES (?, ?, 'trial_change', ?, ?, ?, ?)",
                (trial["ticker"], today, description, materiality, trial["nct_id"],
                 datetime.now().isoformat(timespec="seconds")))
            event_count += cursor.rowcount
    conn.commit()
    conn.close()
    return event_count


# --------------------------------------------------------------------- runner

def run_ctgov(companies, refresh_aliases=True):
    """Full ClinicalTrials.gov step for a list of company rows; returns a summary dict.

    Searches aliases (optional), refreshes trials, takes today's snapshot, and diffs it.
    """
    matched_all, review_all = [], []
    if refresh_aliases:
        for company in companies:
            try:
                matched, unmatched = build_aliases(company["ticker"], company["name"])
                matched_all.extend(matched)
                review_all.extend(unmatched)
            except Exception as error:  # one bad ticker must never stop the run
                log.error("%s: alias search failed (%s)", company["ticker"], error)
        save_aliases(matched_all, review_all)
    trial_count = 0
    for company in companies:
        try:
            trial_count += fetch_trials_for_ticker(company["ticker"])
        except Exception as error:
            log.error("%s: trial fetch failed (%s)", company["ticker"], error)
    today_text = date.today().isoformat()
    take_snapshots(today_text)
    return {"trials": trial_count, "events": run_trial_diff(today_text)}


def get_companies_for_args(tickers_arg):
    """Company rows (ticker, name) for ALL universe companies or a comma list."""
    if tickers_arg == "ALL":
        return run_query("SELECT ticker, name FROM companies WHERE in_universe = 1")
    wanted = tickers_arg.split(",")
    marks = ",".join("?" * len(wanted))
    return run_query(f"SELECT ticker, name FROM companies WHERE ticker IN ({marks})", wanted)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Refresh ClinicalTrials.gov data.")
    parser.add_argument("--tickers", default="ALL", help="ALL or comma-separated tickers")
    parser.add_argument("--skip-aliases", action="store_true", help="reuse saved aliases")
    args = parser.parse_args()
    setup_logging()
    create_tables()
    summary = run_ctgov(get_companies_for_args(args.tickers), refresh_aliases=not args.skip_aliases)
    print(f"Trials saved: {summary['trials']}; trial-change events today: {summary['events']}")
