"""The two trackers, and the only code that touches them.

    Job_Search_Current.xlsx          repo root, next to CVPrabhatTyagi.pdf. Whatever was made last.
                                     Replaced on every publish, the way CVPrabhatTyagi.pdf rotates.
    trackers/Job_Search_Master.xlsx  the compiled record. Applied (one row per tailored CV, Status and
                                     Date applied edited by hand), Not applied (every other posting
                                     ever harvested, one row each, ranked), Target companies, Strategy
                                     archive. Rows are only ever added or updated.

Every publish writes the new sheets to Current and upserts the same rows into Master, so rotating
Current never loses anything. The harvest's dedup reads Master, so a posting is judged once, ever.
"""
import datetime as dt
import os
import re
import tempfile

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CURRENT = os.path.join(ROOT, "Job_Search_Current.xlsx")
MASTER = os.path.join(ROOT, "trackers", "Job_Search_Master.xlsx")

POSTINGS = "Not applied"
APPS = "Applied"
# Names before 2026-10-09. A workbook still carrying them is renamed in place on first open, so the
# hand-edited Status and Date applied columns survive untouched.
LEGACY = {"All postings": POSTINGS, "Applications": APPS}
# Only what is needed to act on a posting. Provenance (first seen, source board, which sheets it
# appeared on), region and contract were dropped at the user's request on 2026-09-30.
POST_COLS = ["Priority", "Tier", "Last seen", "Job title", "Company", "Location", "Language",
             "Link", "Fit note"]
POST_WIDTHS = [18, 11, 11, 46, 26, 24, 9, 46, 100]

# Priority buckets, in sort order. Freshness matters as much as fit: at ~2.8 applicants a day a
# posting is buried after about seven weeks, and most July and August postings are closed.
BUCKETS = ["P1 Apply now", "P2 Strong", "P3 Worth a look", "P4 Old or unjudged",
           "Done (CV made)", "P5 Not for you"]
SKIP_WORDS = ("skip", "killed", "dead", "closed", "too-senior", "too senior", "wrong-location",
              "language-gated", "employer-red-flag")
APP_COLS = ["CV built", "Company", "Role", "Lang", "CV file", "Status", "Date applied",
            "Follow-up", "Link", "Notes", "Source"]
APP_WIDTHS = [11, 24, 40, 6, 52, 12, 12, 16, 46, 60, 30]

HEAD = PatternFill("solid", fgColor="FF1F3864")
TIER_FILL = {"1-strong": "FFD9EAD3", "2-solid": "FFFFF2CC", "2-stretch": "FFFCE5CD"}
PRIORITY_FILL = {"P1 Apply now": "FFB6D7A8", "P2 Strong": "FFD9EAD3", "P3 Worth a look": "FFFFF2CC",
                 "P4 Old or unjudged": "FFEFEFEF", "Done (CV made)": "FFCFE2F3", "P5 Not for you": "FFF4CCCC"}


def _norm():
    import collect  # lazy: collect imports this module from inside Seen, never at load time
    return collect


def key_of(row):
    """Stable identity: the posting URL when it is a real posting, else folded company + title."""
    c = _norm()
    link = str(row.get("Link") or "")
    if link.startswith("http") and "jobs/search" not in link and "?q=" not in link:
        u = c.norm_url(link)
        if u:
            return ("url", u)
    return ("pair", c.norm_company(row.get("Company") or ""), c.norm_title(row.get("Job title") or ""))


def pair_of(row):
    c = _norm()
    return ("pair", c.norm_company(row.get("Company") or ""), c.norm_title(row.get("Job title") or ""))


def tier_of(raw):
    """Fold the ~50 tier spellings used across four harvest eras (P2, B1, Medium, "skip - too
    senior", 1-good...) onto the judge's four grades. Blank means never judged."""
    t = str(raw or "").strip().lower()
    if not t or t in ("none", "note", "live") or any(w in t for w in ("verif", "review")):
        return ""
    if any(w in t for w in SKIP_WORDS):
        return "skip"
    if "stretch" in t or "maybe" in t or t == "low":
        return "2-stretch"
    if "solid" in t or t == "medium":
        return "2-solid"
    if "strong" in t or "lead" in t or t == "top":
        return "1-strong"
    m = re.match(r"(?:p|t|tier)?\s*(\d)", t)
    if m:
        return {"1": "1-strong", "2": "2-solid"}.get(m.group(1), "2-stretch")
    m = re.match(r"([abcd])\d?\b", t)
    if m:
        return {"a": "1-strong", "b": "2-solid", "c": "2-stretch", "d": "skip"}[m.group(1)]
    return "2-stretch"   # judged, but only with a hedge such as "agency-mediated"


def lang_of(raw):
    """'FR posting; English required', 'EN/FR', 'French posting' -> FR, EN or FR+EN."""
    t = str(raw or "").lower()
    fr = bool(re.search(r"\bfr\b|french|fran[cç]ais", t))
    en = bool(re.search(r"\ben\b|english|anglais", t))
    return "FR+EN" if fr and en else ("EN" if en else ("FR" if fr else ""))


def prioritise(rows, done, today):
    """Stamp Priority on every row and sort. `done` holds keys of postings a CV was built for."""
    c = _norm()
    rank = {b: i for i, b in enumerate(BUCKETS)}
    trank = {"1-strong": 0, "2-solid": 1, "2-stretch": 2, "": 3, "skip": 4}
    for r in rows:
        r["Tier"] = tier_of(r.get("Tier"))
        r["Language"] = lang_of(r.get("Language"))
        try:
            age = (today - dt.date.fromisoformat(str(r.get("Last seen"))[:10])).days
        except ValueError:
            age = 999
        region, t = c.region_of(str(r.get("Location") or "")), r["Tier"]
        if key_of(r) in done or pair_of(r) in done:
            p = 4
        elif t == "skip" or region in ("Abroad", "Remote abroad"):
            p = 5
        elif not t or age > 45:
            p = 3
        elif t == "1-strong":
            p = 0 if age <= 21 else 1
        elif t == "2-solid":
            p = 1 if age <= 21 else 2
        else:
            p = 2 if age <= 21 else 3
        r["Priority"] = BUCKETS[p]
        r["_age"] = age
    rows.sort(key=lambda r: (rank[r["Priority"]], trank.get(r["Tier"], 3), r["_age"],
                             str(r.get("Company") or "").lower()))
    for r in rows:
        r.pop("_age", None)
    return rows


def done_keys(wb):
    """Postings a tailored CV already exists for, matched by posting URL or company + role."""
    done = set()
    for a in read_sheet(wb, APPS, APP_COLS):
        row = {"Company": a.get("Company"), "Job title": a.get("Role"), "Link": a.get("Link")}
        done.add(pair_of(row))
        k = key_of(row)
        if k[0] == "url":
            done.add(k)
    return done


def migrate(wb):
    for old, new in LEGACY.items():
        if old in wb.sheetnames and new not in wb.sheetnames:
            wb[old].title = new
    return wb


def read_sheet(wb, name, cols):
    if name not in wb.sheetnames:
        return []
    ws = wb[name]
    head = [str(c.value or "") for c in ws[1]]
    idx = {h: head.index(h) for h in cols if h in head}
    out = []
    for r in ws.iter_rows(min_row=2):
        row = {h: r[i].value for h, i in idx.items()}
        li = idx.get("Link")
        if li is not None and r[li].hyperlink and r[li].hyperlink.target:
            row["Link"] = r[li].hyperlink.target
        if any(v not in (None, "") for v in row.values()):
            out.append(row)
    return out


def write_sheet(wb, name, cols, widths, rows, first=False):
    if name in wb.sheetnames:
        pos = wb.sheetnames.index(name)
        del wb[name]
        ws = wb.create_sheet(name, pos)
    else:
        ws = wb.create_sheet(name, 0 if first else None)
    for i, h in enumerate(cols, 1):
        c = ws.cell(row=1, column=i, value=h)
        c.font = Font(bold=True, color="FFFFFFFF")
        c.fill = HEAD
        ws.column_dimensions[c.column_letter].width = widths[i - 1]
    li = cols.index("Link") + 1 if "Link" in cols else None
    ti = cols.index("Tier") + 1 if "Tier" in cols else None
    pi = cols.index("Priority") + 1 if "Priority" in cols else None
    for r, row in enumerate(rows, 2):
        for i, h in enumerate(cols, 1):
            v = row.get(h)
            c = ws.cell(row=r, column=i, value=v)
            if i == li and isinstance(v, str) and v.startswith("http"):
                c.hyperlink = v
                c.style = "Hyperlink"
        if ti and row.get("Tier") in TIER_FILL:
            ws.cell(row=r, column=ti).fill = PatternFill("solid", fgColor=TIER_FILL[row["Tier"]])
        if pi and row.get("Priority") in PRIORITY_FILL:
            ws.cell(row=r, column=pi).fill = PatternFill("solid", fgColor=PRIORITY_FILL[row["Priority"]])
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    return ws


def save_atomic(wb, path):
    """Write to a temp file, then swap. A crash mid-save leaves the old Master intact."""
    fd, tmp = tempfile.mkstemp(suffix=".xlsx", dir=os.path.dirname(path))
    os.close(fd)
    wb.save(tmp)
    os.replace(tmp, path)


def merge(record, row, when):
    """Fold `row`, seen on date `when`, into `record`. Newer sightings win; blanks never overwrite."""
    newer = not record.get("Last seen") or (when and str(when) >= str(record["Last seen"]))
    for h in ("Tier", "Job title", "Company", "Location", "Region", "Language", "Contract",
              "Source", "Link", "Fit note"):
        v = row.get(h)
        if v in (None, ""):
            continue
        if newer or record.get(h) in (None, ""):
            record[h] = v
    if when:
        record["Last seen"] = max(str(record.get("Last seen") or when), str(when))
        record["First seen"] = min(str(record.get("First seen") or when), str(when))
    found = set(filter(None, str(record.get("Found in") or "").split(" | ")))
    found.update(filter(None, str(row.get("Found in") or "").split(" | ")))
    record["Found in"] = " | ".join(sorted(found))
    return record


def upsert_postings(rows, when=None):
    """Merge posting rows into Master's All postings, then re-rank every row by priority.
    `upsert_postings([])` re-ranks without adding anything. Returns (added, updated)."""
    when = when or dt.date.today().isoformat()
    wb = migrate(load_workbook(MASTER)) if os.path.exists(MASTER) else Workbook()
    if "Sheet" in wb.sheetnames and len(wb.sheetnames) == 1:
        wb.remove(wb["Sheet"])
    existing = read_sheet(wb, POSTINGS, POST_COLS)
    by_key = {}
    for rec in existing:
        by_key[key_of(rec)] = rec
        by_key.setdefault(pair_of(rec), rec)
    added = updated = 0
    for row in rows:
        rec = by_key.get(key_of(row)) or by_key.get(pair_of(row))
        if rec is None:
            rec = {}
            existing.append(rec)
            added += 1
        else:
            updated += 1
        merge(rec, row, when)
        by_key[key_of(rec)] = rec
        by_key.setdefault(pair_of(rec), rec)
    prioritise(existing, done_keys(wb), dt.date.today())
    # A posting with a tailored CV belongs to Applied, so it leaves Not applied. Dedup still sees it
    # through Applied's company + role and link (dedup_index).
    existing = [r for r in existing if r.get("Priority") != "Done (CV made)"]
    write_sheet(wb, POSTINGS, POST_COLS, POST_WIDTHS, existing)
    save_atomic(wb, MASTER)
    return added, updated


def dedup_index():
    """Every posting URL and company+title pair Master has ever held, for the harvest's dedup."""
    urls, pairs = set(), set()
    if not os.path.exists(MASTER):
        return urls, pairs
    wb = migrate(load_workbook(MASTER))
    for row in read_sheet(wb, POSTINGS, POST_COLS):
        k = key_of(row)
        if k[0] == "url":
            urls.add(k[1])
        pairs.add(pair_of(row)[1:])
    for row in read_sheet(wb, APPS, APP_COLS):
        if row.get("Company") and row.get("Role"):
            pairs.add(pair_of({"Company": row["Company"], "Job title": row["Role"]})[1:])
        k = key_of({"Link": row.get("Link"), "Company": row.get("Company"), "Job title": row.get("Role")})
        if k[0] == "url":
            urls.add(k[1])
    return urls, pairs
