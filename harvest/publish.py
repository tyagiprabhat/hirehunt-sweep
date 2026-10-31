#!/usr/bin/env python3
"""Merge the judge's verdicts into the candidates and write the tracker sheet.

    python harvest/publish.py harvest/out/<date> trackers/Harvest_<date>.xlsx

Reads candidates.json + verdicts.jsonl (one JSON object per line from the harvester subagent),
builds the 10-column payload that scripts/write_harvest_sheet.py expects, and writes one sheet
per region with the doctrine's sections: TIER 1 strict apply order (by funnel then freshness),
TIER 2, SKIP kept with reasons, KILLED at collection kept with reasons. The preamble stamps
provenance: date, sources, counts, and today's LinkedIn mint band.
"""
import datetime as dt
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

TIER_ORDER = {"1-strong": 0, "2-solid": 1, "2-stretch": 2, "skip": 3}
FUNNEL_ORDER = {"quiet": 0, "unknown": 1, "open": 2, "buried": 3}


def posted_str(c):
    bits = []
    if c.get("posted"):
        bits.append(c["posted"])
    if c.get("days_old") is not None:
        bits.append("%dd" % c["days_old"])
    if c.get("extra", {}).get("posted_label"):
        bits.append("label '%s'" % c["extra"]["posted_label"])
    bits.append("verified %s" % dt.date.today().strftime("%d %b"))
    return ", ".join(bits)


def appl_str(c):
    a = c.get("applicants")
    if a == -1:
        return "<25 applicants"
    if isinstance(a, int):
        return "%d applicants%s" % (a, " (BURIED)" if c.get("funnel") == "buried" else "")
    return "applicants n/a"


def to_row(c, v):
    fit = v.get("fit", "")
    tail = " | LEVEL: %s | %s" % (v.get("level", "not stated"), appl_str(c))
    if c.get("region") in ("ex-IDF", "TGV commute"):
        tail += " | %s" % c["region"]
    if c.get("lane") == "target":
        tail += " | TARGET tier %s" % c.get("extra", {}).get("target_tier")
    if v.get("tier") == "skip" and v.get("kill"):
        fit = "SKIP: %s. %s" % (v["kill"], fit)
    return [v.get("tier", "skip"), c["title"], c["company"], c["location"], posted_str(c),
            c["source"], v.get("lang", ""), v.get("contract", ""), c["url"], (fit + tail)[:900]]


def main():
    import collect
    import master
    out_dir = sys.argv[1]
    # Current is replaced on every publish, the way CVPrabhatTyagi.pdf rotates. Master keeps
    # everything, so nothing is lost when Current rotates.
    tracker = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else master.CURRENT
    data = json.load(open(os.path.join(out_dir, "candidates.json"), encoding="utf-8"))
    verdicts = {}
    import glob
    lines = []
    for vpath in sorted(glob.glob(os.path.join(out_dir, "verdicts*.jsonl"))):
        lines.extend(open(vpath, encoding="utf-8").read().splitlines())
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            v = json.loads(line)
            verdicts[v["id"]] = v
        except (ValueError, KeyError):
            continue

    s = data["summary"]
    run_date = dt.date.fromisoformat(s.get("date") or dt.date.today().isoformat())
    cands = data["candidates"]
    unjudged = [c for c in cands if c["id"] not in verdicts]
    for c in cands:
        c["_v"] = verdicts.get(c["id"], {"tier": "verify", "fit": "no verdict returned", "level": "", "lang": "", "contract": ""})

    def sheet_for(region_names, name, langs=None):
        rows = [c for c in cands if c["region"] in region_names]
        if langs:
            rows = [c for c in rows if (c["_v"].get("lang") or "FR").upper() in langs]
            # FR+EN first inside each tier: French is still required, but a bilingual posting is
            # the softer French bar and the better use of a B2.
            rows.sort(key=lambda c: 0 if (c["_v"].get("lang") or "").upper() == "FR+EN" else 1)
        rows.sort(key=lambda c: (TIER_ORDER.get(c["_v"]["tier"], 9), FUNNEL_ORDER.get(c.get("funnel"), 1),
                                 c.get("days_old") if c.get("days_old") is not None else 99))
        t1 = [c for c in rows if c["_v"]["tier"] == "1-strong"]
        t2 = [c for c in rows if c["_v"]["tier"] in ("2-solid", "2-stretch")]
        sk = [c for c in rows if c["_v"]["tier"] in ("skip", "verify")]
        for i, c in enumerate(t1, 1):
            c["_v"]["fit"] = "APPLY ORDER #%d. %s" % (i, c["_v"]["fit"])
        killed = [] if (langs and "EN" in langs) else \
            [k for k in data["killed"] if k.get("region", region_of_killed(k)) in region_names or not region_names]
        sections = [
            {"heading": "TIER 1 - STRICT APPLY ORDER (quiet funnel first, then freshest). %d rows" % len(t1),
             "rows": [to_row(c, c["_v"]) for c in t1]},
            {"heading": "TIER 2 - SOLID / STRETCH. %d rows" % len(t2), "rows": [to_row(c, c["_v"]) for c in t2]},
            {"heading": "SKIP - kept with the reason so it is never re-evaluated from scratch. %d rows" % len(sk),
             "rows": [to_row(c, c["_v"]) for c in sk]},
            {"heading": "KILLED AT COLLECTION (excluded employer, hard-reject title/body, stale, duplicate, dead, identity mismatch). %d rows" % len(killed),
             "rows": [["killed", k.get("title", ""), k.get("company", ""), k.get("location", ""), k.get("posted", ""),
                       k.get("source", ""), "", "", k.get("url", ""), k.get("why", "")] for k in killed[:400]]},
        ]
        pre = ("%s. Compiled %s. Sources: LinkedIn guest API (identity + applicants verified on every row), "
               "employer ATS boards (Ashby, Greenhouse, Lever, SmartRecruiters, amazon.jobs), HelloWork, France Travail, "
               "APEC (discovery only). Lanes: %s. %d raw -> %d verified -> %d judged (%d unjudged). "
               "Former employers (Geodis, Sonoco, Ferrero) excluded at source. Deduped against Applications.xlsx and "
               "every prior harvest sheet (%s). LinkedIn mint band today: %s. Tier 1 half-life is 9-11 days: re-read "
               "this queue within a week. Indeed, Glassdoor and Welcome to the Jungle are 403 from this machine and "
               "were not queried.") % (
            name.upper(), run_date.strftime("%A %d %B %Y"), ", ".join(s["lanes"]), s["raw"], s["verified"],
            len(verdicts), len(unjudged), "trackers/seen_postings.json", s["linkedin_mint_band"])
        return {"sheet_name": name, "preamble": pre, "sections": sections}

    payloads = []
    stamp = run_date.strftime("%d %b")
    split_lang = "--split" in sys.argv and sys.argv[sys.argv.index("--split") + 1] == "lang"
    for reg, regions, label in (("idf", {"IDF", "Remote", "France (unspecified)"}, "IDF"),
                                ("exidf", {"ex-IDF", "TGV commute"}, "France ex-IDF")):
        if reg not in s["regions"]:
            continue
        if split_lang:
            payloads.append(sheet_for(regions, "%s English %s" % (label, stamp), {"EN"}))
            payloads.append(sheet_for(regions, "%s French %s" % (label, stamp), {"FR", "FR+EN"}))
        else:
            payloads.append(sheet_for(regions, "%s %s" % (label, stamp)))

    if os.path.abspath(tracker) == os.path.abspath(master.CURRENT) and os.path.exists(tracker):
        os.remove(tracker)
    rows = []
    for p in payloads:
        for sec in p["sections"]:
            for r in sec["rows"]:
                r = list(r) + [None] * (10 - len(r))
                rows.append({"Tier": r[0], "Job title": r[1], "Company": r[2], "Location": r[3],
                             "Source": r[5], "Language": r[6], "Contract": r[7], "Link": r[8],
                             "Fit note": r[9], "Region": collect.region_of(str(r[3] or "")),
                             "Found in": p["sheet_name"]})
    # A run publishes sheets only for the regions it collected, but the collector also returns
    # ex-IDF, TGV-commute and unclassified rows. Those were silently dropped from Master (17 rows
    # on 5 Oct, one of them a 1-strong), so the dedup re-collected and re-judged them later.
    on_sheet = {str(r["Link"] or "").rstrip("/").lower() for r in rows}
    off = [c for c in cands if str(c.get("url") or "").rstrip("/").lower() not in on_sheet]
    for c in off:
        v = c["_v"]
        rows.append({"Tier": v.get("tier"), "Job title": c.get("title"), "Company": c.get("company"),
                     "Location": c.get("location"), "Source": c.get("source"), "Language": v.get("lang"),
                     "Contract": v.get("contract"), "Link": c.get("url"),
                     "Fit note": "%s | %s | not on a sheet: %s" % (v.get("fit", ""), appl_str(c), c.get("region")),
                     "Region": c.get("region"), "Found in": "Harvest %s (off-sheet)" % stamp})
    for p in payloads:
        ppath = os.path.join(out_dir, "payload_%s.json" % p["sheet_name"].replace(" ", "_"))
        json.dump(p, open(ppath, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        subprocess.run([PY, os.path.join(ROOT, "scripts", "write_harvest_sheet.py"), ppath, tracker], check=True)
        print("wrote sheet '%s' -> %s" % (p["sheet_name"], tracker))
    added, updated = master.upsert_postings(rows, run_date.isoformat())
    if off:
        print("master: %d judged rows outside the published regions kept in Master" % len(off))
    print("master: %d postings added, %d updated -> %s" % (added, updated, master.MASTER))
    if unjudged:
        print("WARNING: %d verified candidates had no verdict (marked 'verify')" % len(unjudged))
    # Postings of jobs applied to since the last harvest, for Insights' tool counts. Best effort:
    # a network or login problem must not fail the harvest.
    if subprocess.run([PY, os.path.join(ROOT, "scripts", "fill_postings.py")]).returncode:
        print("note: scripts/fill_postings.py did not finish; run it again by hand")


def region_of_killed(k):
    return "IDF" if any(x in (k.get("location") or "").lower() for x in ("paris", "île-de-france", "ile-de-france")) else "ex-IDF"


if __name__ == "__main__":
    main()
