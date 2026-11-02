#!/usr/bin/env python3
"""Collect, filter, dedupe and verify. Zero LLM tokens.

    python harvest/collect.py [--region idf|exidf|all|france|directory|fresh] [--tag NAME] [--limit N] [--lanes a,b,c] [--quick]

Writes harvest/out/<date>/candidates.json (everything, with verdicts) and judge_input.jsonl
(only the rows that need a human-grade judgment, trimmed to what the judge needs). The judge is
the `harvester` subagent; `publish.py` turns its verdicts into the tracker sheet.

Pipeline, in the order the doctrine puts it:
  1. collect     every lane phrase against every source, plus target employers' own ATS boards
  2. exclude     former employers; hard-reject titles (VIE, stage, alternance, senior, director,
                 production coding, retail floor); stale rows where the source gives a date
  3. dedupe      URL / stable ID, then folded company+title, against trackers/seen_postings.json,
                 trackers/Job_Search_Master.xlsx and everything found earlier in this run
  4. verify      fetch the posting: identity (company AND title, rule 11), liveness, applicants,
                 description; hard-reject bodies (nationality, habilitation, 5+ year bars)
  5. classify    region (IDF / TGV-commute / ex-IDF / remote), junior evidence, senior soft flags,
                 buried (>=150 applicants), quiet (<25)
"""
import argparse
import concurrent.futures
import functools
print = functools.partial(print, flush=True)  # noqa: A001
import datetime as dt
import json
import os
import re
import sys
import time
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sources  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(ROOT, "harvest", "config")
TRK = os.path.join(ROOT, "trackers")
TODAY = dt.date.today()


def load(name):
    with open(os.path.join(CFG, name), encoding="utf-8") as f:
        return json.load(f)


LANES, RULES, TARGETS = load("lanes.json"), load("rules.json"), load("targets.json")
LANES = {k: v for k, v in LANES.items() if not k.startswith("_")}
DIRECTORY = load("directory.json")["companies"]
RX = lambda pats: [re.compile(p, re.I) for p in pats]  # noqa: E731
R_TITLE, R_BODY = RX(RULES["hard_reject_title"]), RX(RULES["hard_reject_body"])
R_SOFT, R_JUNIOR = RX(RULES["senior_soft_flags"]), RX(RULES["junior_evidence"])

IDF_LOC = "Paris, Île-de-France, France"   # "Île-de-France, France" alone returns ~3x fewer cards
# Whole-country mode. A bare "France" search is ranked toward Paris, and many employers sit in
# small towns that no city list will ever name, so each non-IDF metropolitan region is searched
# as its own location. IDF is left out: it has its own sweep and is the densest pool.
LIMIT = 0
FRANCE_SCOPED = False
FRESH_HOURS = 3   # the hourly hunter looks back 3 h, so one missed or late run loses nothing
FRANCE_REGIONS = ["Auvergne-Rhône-Alpes", "Bourgogne-Franche-Comté", "Bretagne", "Centre-Val de Loire",
                  "Grand Est", "Hauts-de-France", "Normandie", "Nouvelle-Aquitaine", "Occitanie",
                  "Pays de la Loire", "Provence-Alpes-Côte d'Azur", "Corse"]
EXIDF_CITIES = ["Lyon", "Bordeaux", "Nantes", "Lille", "Toulouse", "Marseille", "Strasbourg", "Rennes", "Grenoble"]
FT_LIEUX = {"idf": ["75D", "92D", "93D", "94D", "78D", "91D", "95D", "77D"],
            "exidf": ["69D", "33D", "44D", "59D", "31D", "13D", "67D", "35D", "38D"]}
APEC_LIEUX = {"idf": ["75", "77", "78", "91", "92", "93", "94", "95"],
              "exidf": ["69", "33", "44", "59", "31", "13", "67", "35", "38"]}


# --------------------------------------------------------------------------- normalisation
def fold(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def norm_company(s):
    s = re.split(r"[(–\-,|]", fold(s))[0]
    return re.sub(r"[^a-z0-9]", "", s)[:14]


_NOISE = {"france", "group", "groupe", "sa", "sas", "sasu", "international", "holding", "the", "de", "des",
          "du", "et", "and", "industries", "industrie", "company", "co", "inc", "ltd", "gmbh", "ag", "plc", "nv"}


def core_name(s):
    """'Accenture France' and 'Accenture' must compare equal: norm_company cuts at 14 letters, so the
    suffix 'France' alone broke the match for most of the directory."""
    toks = [t for t in fold(s).replace("'", " ").split() if t not in _NOISE]
    return "".join(toks)


def same_company(a, b):
    x, y = core_name(a), core_name(b)
    if not x or not y:
        return False
    if x == y:
        return True
    # one name containing the other ("L'Oreal Luxe" / "L'Oreal") only when the shorter is distinctive
    short, long_ = sorted((x, y), key=len)
    return len(short) >= 5 and long_.startswith(short)


def tag_france(r):
    """A search scoped to France returns small towns with no country ('Clermont-Ferrand'), which
    region_of files under Unknown, and a target row in Unknown is killed. Say France where the
    location has no foreign marker, so a posting in a small town is not lost for being small."""
    loc = fold(r.get("location") or "")
    if loc and "france" not in loc and not any(k in " " + loc + " " for k in FOREIGN):
        r["location"] = (r["location"] or "").strip() + ", France"


def norm_title(s):
    s = fold(s)
    s = re.sub(r"\b(h/f/x|f/h/x|h/f|f/h|m/f|f/m|m/w/d|nb|cdi|cdd|x)\b", " ", s)
    return re.sub(r"[^a-z0-9]", "", s)[:22]


def norm_url(u):
    u = (u or "").split("?")[0].rstrip("/").lower()
    m = re.search(r"linkedin\.com/jobs/view/(?:.*-)?(\d+)", u)
    return "li:" + m.group(1) if m else u


FOREIGN = ["united states", "usa", "new york", "seattle", "austin", "palo alto", "san francisco", "boston", "chicago",
           "india", "bengaluru", "bangalore", "hyderabad", "pune", "mumbai", "chennai", "gurgaon", "noida",
           "germany", "berlin", "munich", "hamburg", "deutschland", "spain", "madrid", "barcelona", "italy", "milan", "milano",
           "united kingdom", "london", "ireland", "dublin", "netherlands", "amsterdam", "belgium", "brussels", "bruxelles",
           "sweden", "stockholm", "denmark", "copenhagen", "poland", "warsaw", "portugal", "lisbon", "switzerland", "zurich",
           "canada", "toronto", "montreal", "singapore", "australia", "sydney", "japan", "tokyo", "brazil", "mexico", "dubai",
           "luxembourg", "romania", "bucharest", "czech", "prague", "austria", "vienna", "israel", "tel aviv", "egypt", "cairo",
           "morocco", "casablanca", "tunis", "philippines", "manila", "malaysia", "indonesia", "korea", "seoul", "china",
           "shanghai", "hong kong", "remote - us", "remote us", "us remote"]
FRENCH_CITIES = ["lyon", "marseille", "toulouse", "nice", "nantes", "montpellier", "strasbourg", "bordeaux", "lille", "rennes",
                 "reims", "toulon", "grenoble", "dijon", "angers", "nimes", "clermont", "le mans", "aix", "brest", "tours",
                 "amiens", "limoges", "annecy", "perpignan", "metz", "besancon", "orleans", "rouen", "mulhouse", "caen", "nancy",
                 "avignon", "poitiers", "pau", "la rochelle", "valence", "chambery", "lorient", "quimper", "cholet", "roanne",
                 "castres", "libourne", "merignac", "blagnac", "villeurbanne", "ecully"]


def _place_key(s):
    """Fold a place name so 'Fontenay-sous-Bois' and 'fontenay sous bois' compare equal."""
    s = fold(s).replace("-", " ").replace("'", " ").replace("\u2019", " ")
    s = re.sub(r"\bst\b", "saint", s)
    s = re.sub(r"\bste\b", "sainte", s)
    return re.sub(r"\s+", " ", s).strip()


# Every Ile-de-France commune (geo.api.gouv.fr). The hand-kept idf_markers list covers ~50 towns,
# so bare suburb names such as Drancy, Villejuif or Bobigny fell through to "Unknown", and
# "Drancy, France" fell through to "ex-IDF", and either way the row never reached the IDF sheet.
# 169 of 801 verified rows on 2026-09-30 were lost that way.
_COMMUNES = json.load(open(os.path.join(ROOT, "harvest", "config", "idf_communes.json"), encoding="utf-8"))
IDF_COMMUNES = {_place_key(n) for names in _COMMUNES["communes"].values() for n in names}


def _leading_place(location):
    """The town part of 'Torcy - 77', 'Drancy, France', 'Lyon 01 - 69', 'Meaux et peripherie'."""
    head = re.split(r",| - |\(", location or "", maxsplit=1)[0]
    head = re.sub(r"\bet p[ée]riph[ée]rie\b", "", head, flags=re.I)
    head = re.sub(r"\s\d{1,2}(e|er|eme)?$", "", head.strip())
    return _place_key(head)


def region_of(location):
    loc = " " + fold(location).replace("\n", " ") + " "
    if any(k in loc for k in ("remote", "teletravail", "anywhere in france", "france (remote)")):
        if any(k in loc for k in FOREIGN):
            return "Remote abroad"
        return "Remote" if ("france" in loc or "europe" in loc or "emea" in loc or loc.strip() == "remote") else "Remote abroad"
    if any(k in loc for k in RULES["idf_markers"]):
        return "IDF"
    depts = "|".join(RULES["idf_departments"])
    # "Hauts-de-Seine - 92", "Thiais - 94", "94 - Thiais", "(92)", "Paris 3e - 75"
    if re.search(r"(?:^|[\s(,])(%s)(?:[\s),]|$)" % depts, loc.replace(" - ", " ")):
        return "IDF"
    # Exact match on the leading place only, never a substring: commune names include short
    # common words ("Us", "Buc", "Orly") that would match inside unrelated strings. This runs
    # before the fallbacks below, one of which files anything containing "france" as ex-IDF.
    if _leading_place(location) in IDF_COMMUNES:
        return "IDF"
    if any(k in loc for k in RULES["tgv_commute_cities"]):
        return "TGV commute"
    # Kept, not abroad (Prabh, 2026-10-31: "Include, flag sponsorship"): the judge flags each one
    # for a sponsorship check and ranks it below an equal French job.
    if " luxembourg " in loc.replace(",", " "):
        return "Luxembourg"
    if any(k in loc for k in FOREIGN):
        return "Abroad"
    if any(k in loc for k in FRENCH_CITIES) or re.search(r"(?:^|[\s(,])\d{2}(?:[\s),]|$)", loc.replace(" - ", " ")):
        return "ex-IDF"
    if loc.strip() in ("france", "fr", "paris, fr") or "france" in loc:
        return "France (unspecified)" if loc.strip() in ("france", "fr") else "ex-IDF"
    return "Unknown"


def days_old(posted):
    if not posted or not re.match(r"\d{4}-\d{2}-\d{2}", posted):
        return None
    try:
        return (TODAY - dt.date.fromisoformat(posted[:10])).days
    except ValueError:
        return None


# --------------------------------------------------------------------------- dedup index
class Seen:
    def __init__(self):
        self.urls, self.pairs = set(), set()
        path = os.path.join(TRK, "seen_postings.json")
        if os.path.exists(path):
            for s in json.load(open(path, encoding="utf-8")):
                if s.get("link"):
                    self.urls.add(norm_url(s["link"]))
                if s.get("title") and s.get("company"):
                    self.pairs.add((norm_company(s["company"]), norm_title(s["title"])))
        # Master holds every posting ever published and every application, so a posting is
        # judged once, ever. Judging is the only part of a sweep that costs tokens.
        import master
        urls, pairs = master.dedup_index()
        self.urls |= urls
        self.pairs |= pairs
        # Away from the laptop (GitHub Actions) there is no Master: the Worker holds the record.
        import remote
        for url, company, title in remote.seen():
            if url:
                self.urls.add(norm_url(url))
            if company and title:
                self.pairs.add((norm_company(company), norm_title(title)))
        self.loaded = (len(self.urls), len(self.pairs))

    def check(self, c):
        u = norm_url(c["url"])
        p = (norm_company(c["company"]), norm_title(c["title"]))
        if u and u in self.urls:
            return "seen: url"
        if c["company"] and c["title"] and p in self.pairs:
            return "seen: company+title"
        return None

    def add(self, c):
        u = norm_url(c["url"])
        if u:
            self.urls.add(u)
        if c["company"] and c["title"]:
            self.pairs.add((norm_company(c["company"]), norm_title(c["title"])))


# --------------------------------------------------------------------------- collect
def note(label, rows, cands, log):
    line = "%-52s %3d rows" % (label, len(rows))
    log.append(line)
    print("  %s  (total %d)" % (line, len(cands)), flush=True)


def collect(regions, lanes, quick):
    cands, log = [], []
    pages = 3 if quick else 6   # 10 cards per page; 6 keeps the burst under LinkedIn's throttle

    def add(rows, lane, note):
        for r in rows:
            r["lane"] = lane
            if "france" in regions:
                tag_france(r)
        cands.extend(rows)
        line = "%-52s %3d rows" % (note, len(rows))
        log.append(line)
        # Collection used to print nothing until it returned, so a slow sweep and a hung one
        # looked identical from outside. Now every query says so as it lands.
        print("  %s  (total %d)" % (line, len(cands)), flush=True)

    if "fresh" in regions:
        # The hourly hunter (harvest/HUNT.md): every lane phrase, one page, only postings from the
        # last FRESH_HOURS, across France and Luxembourg. LinkedIn only: the guest API is the one
        # source that filters by the hour. Small enough to run every hour without a throttle.
        hours = FRESH_HOURS / 24
        for lane in lanes:
            spec = LANES[lane]
            for ph in spec["fr"] + spec["en"]:
                rows = sources.linkedin_search(ph, "France", hours, 1)
                for r in rows:
                    tag_france(r)
                add(rows, lane, "LinkedIn France %dh '%s'" % (FRESH_HOURS, ph))
            for ph in spec["en"]:
                add(sources.linkedin_search(ph, "Luxembourg", hours, 1), lane, "LinkedIn Luxembourg %dh '%s'" % (FRESH_HOURS, ph))
        return cands, log

    if "directory" in regions:
        # One company at a time: a bare name search, then the name with the role words that are
        # actually applied to, because a large employer has far more than one page of postings.
        suffixes = ["", "supply chain", "analyst", "operations", "project", "data"]
        comps = DIRECTORY[:LIMIT] if LIMIT else DIRECTORY
        for ci, comp in enumerate(comps, 1):
            kept = 0
            for suf in suffixes[:2] if quick else suffixes:
                q = ("%s %s" % (comp["company"], suf)).strip()
                rows = sources.linkedin_search(q, "France", RULES["freshness_days"], 2 if not suf else 1)
                rows = [r for r in rows if same_company(r["company"], comp["company"])]
                for r in rows:
                    tag_france(r)
                    r["lane"] = "target"
                    r["extra"]["target_tier"] = 3
                    r["extra"]["directory"] = comp["industry"]
                cands.extend(rows)
                kept += len(rows)
            line = "%-52s %3d rows  [%d/%d]" % ("Directory '%s'" % comp["company"], kept, ci, len(comps))
            log.append(line)
            print("  %s  (total %d)" % (line, len(cands)), flush=True)
        return cands, log

    # target employers: own ATS boards enumerated in full, then judged like any other row
    for t in TARGETS["ats"]:
        rows = sources.ATS[t["type"]](t["slug"], t["company"])
        for r in rows:
            r["lane"] = "target"
            r["extra"]["target_tier"] = t["tier"]
        cands.extend(rows)
        note("ATS %s/%s" % (t["type"], t["slug"]), rows, cands, log)
    if TARGETS.get("amazon_jobs", {}).get("enabled"):
        for q in TARGETS["amazon_jobs"]["queries"]:
            rows = sources.amazon_jobs(q, TARGETS["amazon_jobs"]["location"])
            for r in rows:
                r["lane"] = "target"
                r["extra"]["target_tier"] = 1
            cands.extend(rows)
            note("amazon.jobs '%s'" % q, rows, cands, log)
    for t in TARGETS["linkedin"]:
        rows = [r for r in sources.linkedin_search(t["company"], "France", 14, 1)
                if norm_company(r["company"]) == norm_company(t["company"]) or same_company(r["company"], t["company"])]
        for r in rows:
            tag_france(r)
            r["lane"] = "target"
            r["extra"]["target_tier"] = t["tier"]
        cands.extend(rows)
        note("LinkedIn target '%s'" % t["company"], rows, cands, log)

    for lane in lanes:
        spec = LANES[lane]
        phrases = spec["en"] if "france" in regions else spec["fr"] + spec["en"]
        if quick:
            phrases = phrases[:3]
        for ph in phrases:
            if "france" in regions:
                add(sources.linkedin_search(ph, "France", RULES["freshness_days"], pages), lane, "LinkedIn France '%s'" % ph)
                for reg_name in (FRANCE_REGIONS[:3] if quick else FRANCE_REGIONS):
                    add(sources.linkedin_search(ph, reg_name + ", France", RULES["freshness_days"], 2), lane,
                        "LinkedIn %s '%s'" % (reg_name, ph))
            if "idf" in regions:
                add(sources.linkedin_search(ph, IDF_LOC, RULES["freshness_days"], pages), lane, "LinkedIn IDF '%s'" % ph)
            if "exidf" in regions:
                for city in (EXIDF_CITIES[:4] if quick else EXIDF_CITIES[:6]):
                    add(sources.linkedin_search(ph, city + ", France", RULES["freshness_days"], 1), lane, "LinkedIn %s '%s'" % (city, ph))
        # HelloWork / France Travail / APEC: French phrases only, they are French boards
        for ph in ([] if "france" in regions else spec["fr"][:2 if quick else 3]):
            for reg in regions:
                for lieu in FT_LIEUX[reg][:2 if quick else 2]:
                    add(sources.francetravail_search(ph, lieu), lane, "FranceTravail %s '%s'" % (lieu, ph))
                if "APEC" not in RULES.get("exclude_sources", []):
                    add(sources.apec_search(ph, APEC_LIEUX[reg]), lane, "APEC %s '%s'" % (reg, ph))
            time.sleep(0.3)

    return cands, log


# --------------------------------------------------------------------------- filter + verify
RELEVANT = re.compile(
    r"analy|planner|planif|planning|buyer|acheteur|approvision|supply|logisti|procurement|achats|ERP|Odoo|SAP|consultant|"
    r"support|op[ée]rations|ops\b|customer|client|order|commande|ADV|implementation|d[ée]ploiement|deployment|solutions?\b|"
    r"revops|revenue op|enablement|automation|automatisation|\bIA\b|\bAI\b|no.?code|coordinat|project|projet|process|"
    r"pre.?sales|avant.?vente|sales engineer|"
    r"data|\bBI\b|reporting|inventory|stock|demand|S&OP|forecast|pr[ée]vision|business|program", re.I)


def clearance_employer(company):
    """rules.json exclude_clearance_employers: a folded substring, or '=name' for the whole core name."""
    f, core = fold(company), core_name(company)
    for x in RULES.get("exclude_clearance_employers", []):
        if (core == x[1:].replace(" ", "")) if x.startswith("=") else (x in f):
            return True
    return False


def cheap_filter(c):
    c["title"] = re.sub(r"\s+", " ", c["title"] or "").strip()
    c["company"] = re.sub(r"\s+", " ", c["company"] or "").strip()
    if any(x in fold(c["company"]) for x in RULES["exclude_employers"]):
        return "excluded employer"
    if clearance_employer(c["company"]):
        return "clearance employer (defence/nuclear/state)"
    if c["lane"] == "target":
        reg = region_of(c["location"])
        if reg in ("Abroad", "Remote abroad", "Unknown"):
            return "abroad/unknown: " + (c["location"] or "?")[:40]
        if not RELEVANT.search(c["title"] or ""):
            return "target board: title outside every lane"
    t = c["title"] or ""
    for rx in R_TITLE:
        if rx.search(t):
            return "title: " + rx.pattern
    d = days_old(c["posted"])
    if d is not None and d > RULES["freshness_days"] and c["lane"] != "target":
        return "stale: %d days" % d
    return None


def verify(c):
    """Fetch the posting where the source has a detail route. Returns (ok, reason)."""
    src = c["source"]
    if src in sources.DETAIL:
        d = sources.DETAIL[src](c["url"])
        c["extra"]["detail_status"] = d.get("status")
        if not d.get("alive", False):
            return False, "dead/closed (HTTP %s)" % d.get("status")
        # identity: company AND title must match what the search card claimed (rule 11)
        if c["company"] and d.get("company") and norm_company(c["company"]) != norm_company(d["company"]):
            return False, "identity mismatch: card '%s' vs page '%s'" % (c["company"], d["company"])
        if c["title"] and d.get("title") and norm_title(c["title"])[:10] != norm_title(d["title"])[:10]:
            return False, "identity mismatch: card '%s' vs page '%s'" % (c["title"], d["title"])
        for k in ("title", "company", "location"):
            if d.get(k):
                c[k] = d[k]
        c["applicants"] = d.get("applicants")
        c["description"] = d.get("description", "")
        c["extra"]["posted_label"] = d.get("posted_label", "")
        c["extra"]["criteria"] = d.get("criteria", [])
        if d.get("posted_iso") and not c.get("posted"):
            c["posted"] = d["posted_iso"]
        dd = days_old(c.get("posted"))
        if dd is not None and dd > RULES["freshness_days"] and c["lane"] != "target":
            return False, "stale after detail: %d days" % dd
    elif "detail_api" in c["extra"]:
        c["description"] = sources.smartrecruiters_detail(c["extra"]["detail_api"])
    body = (c["title"] or "") + "\n" + (c["description"] or "")
    for rx in R_BODY:
        m = rx.search(body)
        if m:
            return False, "body: '%s'" % m.group(0)
    return True, None


def classify(c):
    body = (c["title"] or "") + "\n" + (c["description"] or "")
    # Verification replaces the search card's location with the posting page's own, which drops the
    # France tag again, so a France-scoped run tags once more before classifying.
    if FRANCE_SCOPED:
        tag_france(c)
    c["region"] = region_of(c["location"])
    if c["region"] in ("Abroad", "Remote abroad"):
        return "abroad: " + (c["location"] or "?")[:40]
    c["junior_evidence"] = sorted({m.group(0) for rx in R_JUNIOR for m in rx.finditer(body)})[:4]
    c["senior_soft"] = sorted({m.group(0) for rx in R_SOFT for m in rx.finditer(body)})[:2]
    a = c.get("applicants")
    c["funnel"] = ("buried" if isinstance(a, int) and a >= RULES["buried_applicants"]
                   else "quiet" if a == -1 or (isinstance(a, int) and 0 <= a < RULES["quiet_applicants"])
                   else "unknown" if a is None else "open")
    c["days_old"] = days_old(c["posted"])
    c["lang"] = post_lang(body)
    c["wants_english"] = bool(EN_REQUIRED.search(body))
    # The four buckets asked for on 2026-09-28. A French-language posting only earns a bucket
    # when it also asks for English; French-only postings are kept but parked in "FR only".
    zone = "IDF" if c["region"] in ("IDF", "TGV commute") else "Luxembourg" if c["region"] == "Luxembourg" else "ex-IDF"
    if c["lang"] == "en":
        c["bucket"] = zone + " / EN"
    elif c["wants_english"]:
        c["bucket"] = zone + " / FR+EN"
    else:
        c["bucket"] = zone + " / FR only"


# --------------------------------------------------------------------------- posting language
# Words that only one of the two languages uses, so a body mixing a French preamble with an
# English job title still lands on the language it is actually written in.
FR_ONLY = re.compile(r"\b(vous|nous|votre|notre|des|les|une|pour|avec|dans|sur|chez|au sein|"
                     r"qui|que|plus|sera|sont|poste|mission|missions|profil|recherche|"
                     r"[ée]quipe|entreprise|comp[ée]tences|exp[ée]rience)\b", re.I)
EN_ONLY = re.compile(r"\b(you|your|we|our|the|and|with|will|are|have|this|that|from|"
                     r"role|team|about|looking|experience|skills|requirements)\b", re.I)
# "English required" as an employer writes it, in either language. A bare "English" in a
# language list is enough: the point is the job is not French-only.
EN_REQUIRED = re.compile(r"\banglais\b|\benglish\b|\bbilingue\b|\bbilingual\b", re.I)


def post_lang(body):
    fr, en = len(FR_ONLY.findall(body)), len(EN_ONLY.findall(body))
    return "fr" if fr > en else "en"


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="all", choices=["idf", "exidf", "all", "france", "directory", "fresh"])
    ap.add_argument("--tag", default="", help="suffix for the output folder, so a second run on the same day cannot overwrite the first")
    ap.add_argument("--lanes", default=",".join(LANES.keys()))
    ap.add_argument("--limit", type=int, default=0, help="directory mode: only the first N companies, for a smoke test")
    ap.add_argument("--quick", action="store_true", help="fewer phrases and pages, for a smoke test")
    ap.add_argument("--from-raw", metavar="PATH", help="reuse a previous run's raw.json instead of re-collecting")
    a = ap.parse_args()
    regions = ["idf", "exidf"] if a.region == "all" else [a.region]
    # "france" is the whole country in one LinkedIn search per English phrase, not IDF plus six
    # named cities. It always gets its own folder: a same-day run into harvest/out/<date> would
    # overwrite the raw.json that fill_postings.py and the published sheet were built from.
    tag = a.tag or {"france": "france_en", "directory": "directory",
                    "fresh": "fresh_" + dt.datetime.now(dt.timezone.utc).strftime("%H%M")}.get(a.region, "")
    lanes = [x for x in a.lanes.split(",") if x in LANES]
    global LIMIT, FRANCE_SCOPED
    LIMIT = a.limit
    FRANCE_SCOPED = a.region in ("france", "directory", "fresh")

    out_dir = os.path.join(ROOT, "harvest", "out", TODAY.isoformat() + ("_" + tag if tag else ""))
    os.makedirs(out_dir, exist_ok=True)
    t0 = time.time()

    if a.from_raw:
        raw = json.load(open(a.from_raw, encoding="utf-8"))
        cands, log = raw["candidates"], raw["queries"]
        print("reused %d raw rows from %s" % (len(cands), a.from_raw))
    else:
        cands, log = collect(regions, lanes, a.quick)
        print("collected %d raw rows from %d queries in %.0fs" % (len(cands), len(log), time.time() - t0))
        # Collection is the expensive hour. Land it on disk BEFORE anything that can raise,
        # so a failure in dedup, verify or publish costs a re-run of those, not of the sweep.
        json.dump({"candidates": cands, "queries": log},
                  open(os.path.join(out_dir, "raw.json"), "w", encoding="utf-8"),
                  ensure_ascii=False)
        print("-> raw.json saved (%d rows)" % len(cands))

    seen = Seen()
    print("dedup index: %d urls, %d company+title pairs already seen" % seen.loaded)

    kept, killed = [], []
    for c in cands:
        why = cheap_filter(c) or seen.check(c)
        if why:
            c["verdict"], c["why"] = "killed", why
            killed.append(c)
            continue
        seen.add(c)
        kept.append(c)
    print("after cheap filters + dedup: %d to verify, %d killed" % (len(kept), len(killed)))

    # Detail fetching is pure network wait, so run it on a small pool. Four workers is the point
    # where LinkedIn stops answering politely; the per-call DELAY stays as the per-worker pacing.
    verified, done = [], [0]

    def check(c):
        ok, why = verify(c)
        if ok:
            why = classify(c)
        c["verdict"], c["why"] = ("candidate", None) if (ok and not why) else ("killed", why)
        time.sleep(sources.DELAY if c["source"] in sources.DETAIL else 0.05)
        return c

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for c in pool.map(check, kept):
            (verified if c["verdict"] == "candidate" else killed).append(c)
            done[0] += 1
            if done[0] % 50 == 0:
                print("  verified %d/%d (%d alive)" % (done[0], len(kept), len(verified)))

    mint = max((c["extra"].get("li_id", 0) for c in cands), default=0)
    summary = {
        "date": TODAY.isoformat(), "regions": regions, "lanes": lanes, "quick": a.quick,
        "raw": len(cands), "verified": len(verified), "killed": len(killed),
        "linkedin_mint_band": mint, "seconds": round(time.time() - t0),
        "by_source": {}, "by_region": {}, "by_lane": {}, "by_bucket": {},
    }
    for c in verified:
        for k, v in (("by_source", c["source"]), ("by_region", c["region"]), ("by_lane", c["lane"]),
                     ("by_bucket", c["bucket"])):
            summary[k][v] = summary[k].get(v, 0) + 1

    json.dump({"summary": summary, "candidates": verified, "killed": killed, "queries": log},
              open(os.path.join(out_dir, "candidates.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    with open(os.path.join(out_dir, "judge_input.jsonl"), "w", encoding="utf-8") as f:
        for c in verified:
            f.write(json.dumps({k: c.get(k) for k in (
                "id", "lane", "region", "title", "company", "location", "posted", "days_old", "url",
                "source", "applicants", "funnel", "junior_evidence", "senior_soft",
                "lang", "wants_english", "bucket")}
                | {"target_tier": c["extra"].get("target_tier"), "salary": c["extra"].get("salary"),
                   "description": (c.get("description") or "")[:1200]},
                ensure_ascii=False) + "\n")
    print(json.dumps(summary, indent=1, ensure_ascii=False))
    print("-> %s" % out_dir)


if __name__ == "__main__":
    main()
