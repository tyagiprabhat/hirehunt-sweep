#!/usr/bin/env python3
"""Send a sweep to the Cloudflare Worker, where /jobs shows it on the phone.

    python harvest/push.py harvest/out/<date>

The Worker-side counterpart of publish.py: candidates.json + verdicts*.jsonl, one row a posting.
Rows with no verdict yet (the daily Action judges nothing) go up as 'unjudged', and a later
push of the same folder with verdicts replaces them; and rows killed after a fetch (dead,
identity mismatch, body gates) as 'killed', so tomorrow's sweep skips them as Master's dedup
would. Rows killed by the cheap title filters are not sent: they cost nothing to kill again.
Needs HARVEST_API and HARVEST_TOKEN (harvest/remote.py).
"""
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect  # noqa: E402
import remote  # noqa: E402
from publish import appl_str  # noqa: E402

FETCHED_KILLS = ("dead/closed", "identity mismatch", "body:", "stale after detail", "abroad: ")


def key(c):
    u = collect.norm_url(c.get("url"))
    return u or "pair:%s|%s" % (collect.norm_company(c.get("company")), collect.norm_title(c.get("title")))


# The tools and skills on his CV, as an employer writes them. Shown on /jobs as chips, so he can
# see at a glance what a posting asks for that he already has. Display only: nothing is judged on it.
STACK = [("Excel", r"\bexcel\b|\bVBA\b"), ("Power BI", r"power\s?bi"), ("SQL", r"\bSQL\b"), ("SAP", r"\bSAP\b"),
         ("Odoo", r"\bodoo\b"), ("ERP", r"\bERP\b"), ("HubSpot", r"hubspot"), ("Salesforce", r"salesforce"),
         ("Python", r"\bpython\b"), ("n8n", r"\bn8n\b"), ("Jira", r"\bjira\b"), ("Power Automate", r"power\s?automate"),
         ("Tableau", r"\btableau\b"), ("Looker", r"\blooker\b"), ("Zapier", r"\bzapier\b"), ("Make", r"\bmake\.com\b"),
         ("Notion", r"\bnotion\b(?! de)"), ("Gong", r"\bgong\b"), ("Aircall", r"\baircall\b"), ("Clay", r"\bclay\b"),
         ("Cegid", r"\bcegid\b"), ("Docuware", r"docuware"), ("S&OP", r"S&OP|S&OE|\bIBP\b"), ("CRM", r"\bCRM\b"),
         ("Google Sheets", r"google sheets"), ("AI / LLM", r"\bLLM\b|\bGPT\b|\bIA g[ée]n[ée]rative|\bgenerative AI|\bgen ?AI\b")]
STACK_RX = [(name, re.compile(rx, re.I)) for name, rx in STACK]

# Things on a posting worth seeing before applying. Display only; the hard gates stay in rules.json.
FLAGS = [("native French", r"(fran[çc]ais|french)\s*(langue maternelle|natif|native)|native\s+(french|fran[çc]ais)|langue maternelle fran"),
         ("native English", r"native\s+english|english\s+native|anglais\s+(langue maternelle|natif)"),
         ("other language", r"\b(german|allemand|spanish|espagnol|italian|italien|dutch|n[ée]erlandais|mandarin|japanese|japonais)\b[^.]{0,40}\b(required|requis|exig|courant|fluent|obligatoire|indispensable)"),
         ("EU nationality", r"\b(EU|european)\s+(citizen|national|passport)|ressortissant\s+de\s+l.union"),
         ("driving licence", r"permis\s+B|driving\s+licen[cs]e|driver.s\s+licen[cs]e"),
         ("frequent travel", r"d[ée]placements?\s+fr[ée]quents|frequent\s+travel|travel\s+(up\s+to\s+)?\d{2}\s?%"),
         ("management", r"\bmanage\s+a\s+team\b|encadrement\s+d.une\s+[ée]quipe|management\s+d.une\s+[ée]quipe")]
FLAGS_RX = [(name, re.compile(rx, re.I)) for name, rx in FLAGS]

_K = r"(\d{2,3}(?:[.,]\d)?)\s?k"
_FULL = r"(\d{2,3}[\s\u202f\xa0.,]?\d{3})(?:[.,]00)?"
_CUR = r"(?:\s?(?:€|eur(?:os?)?\b|k€))"
SALARY_RX = [
    re.compile(r"(\d{2,3}(?:[.,]\d)?)\s?k?\s?€?\s*(?:-|à|to|–|et|and)\s*" + _K + r"\s?(?:€|eur)?", re.I),  # 45k-55k €, 45 à 55 k€
    re.compile(_FULL + _CUR + r"?\s*(?:-|à|to|–|et)\s*" + _FULL + _CUR, re.I),              # 45 000 - 55 000 €
    re.compile(r"(?:€|eur)\s?" + _FULL + r"\s*(?:-|to|–)\s*(?:€|eur)?\s?" + _FULL, re.I),  # €45,000 - €55,000
    re.compile(_K + r"\s?(?:€|eur)", re.I),                                                  # 50k€
    re.compile(_FULL + _CUR, re.I),                                                           # 50 000 €
]
YEARS_RX = re.compile(
    r"(?:(\d{1,2})\s*(?:\+|à|a|-|–|to)\s*)?(\d{1,2})\s*\+?\s*(?:ans|an|years?|yrs)\b[^.\n]{0,40}?(?:d.exp|exp[ée]rience|experience)"
    r"|(?:exp[ée]rience|experience)[^.\n]{0,30}?(?:(\d{1,2})\s*(?:\+|à|a|-|–|to)\s*)?(\d{1,2})\s*\+?\s*(?:ans|an|years?|yrs)\b", re.I)
SENIORITY = {"internship": "Internship", "stage": "Internship", "entry level": "Entry level", "premier emploi": "Entry level",
             "associate": "Associate", "mid-senior level": "Mid-Senior", "confirmé": "Mid-Senior", "director": "Director",
             "directeur": "Director", "executive": "Executive", "cadre dirigeant": "Executive"}
# LinkedIn's "Employment type" and Ashby's employmentType. "Contract" and "Temporary" stay as written:
# neither says CDD or freelance, and a guess here would read as a fact on the card.
EMPLOYMENT = {"contract": "Contract", "temporary": "Temporary", "freelance": "Freelance", "cdi": "CDI", "cdd": "CDD"}


def _amount(s):
    """'45 000' -> 45, '45.5' (k) -> 45. Thousands of EUR a year, or None when it is not a salary."""
    digits = re.sub(r"[\s\u202f\xa0]", "", s)
    if re.search(r"[.,]\d{3}$", digits):
        digits = re.sub(r"[.,]", "", digits)
    try:
        n = float(digits.replace(",", "."))
    except ValueError:
        return None
    if n >= 1000:
        n = n / 1000
    return int(n) if 15 <= n <= 300 else None


def salary_of(text):
    """(label, low in kEUR) for the first salary a posting states, or ('', None).
    Day rates and hourly pay fall outside 15-300k and are left out, rather than misread."""
    for rx in SALARY_RX:
        for m in rx.finditer(text or ""):
            nums = [g for g in m.groups() if g and re.search(r"\d", g)]
            ks = [_amount(g) for g in nums]
            if not ks or any(k is None for k in ks):
                continue
            tail = (text[m.end():m.end() + 25]).lower()
            if re.match(r"\s*(/|par|per)\s*(jour|day|heure|hour|h\b|mois|month)", tail):
                continue
            lo, hi = min(ks), max(ks)
            return ("%dk EUR" % lo if lo == hi else "%d-%dk EUR" % (lo, hi)), lo
    return "", None


def years_of(text):
    """The experience a posting asks for, as '2-3 yrs' or '3+ yrs', or ''."""
    for m in YEARS_RX.finditer(text or ""):
        lo, hi = (m.group(1), m.group(2)) if m.group(2) else (m.group(3), m.group(4))
        if not hi or int(hi) > 15:
            continue
        if lo and int(lo) < int(hi):
            return "%s-%s yrs" % (lo, hi)
        return "%s+ yrs" % hi
    return ""


def work_mode_of(c, text):
    loc = (c.get("location") or "").lower()
    if c.get("extra", {}).get("remote") is True or c.get("region") == "Remote" or "(remote)" in loc:
        return "Remote"
    if "(hybrid)" in loc or "(hybride)" in loc:
        return "Hybrid"
    if "(on-site)" in loc or "(sur site)" in loc:
        return "On-site"
    if re.search(r"full(y)?[\s-]remote|100\s?%\s*(remote|t[ée]l[ée]travail)|t[ée]l[ée]travail\s+(complet|total|int[ée]gral)|remote[\s-]first", text, re.I):
        return "Remote"
    if re.search(r"hybrid|hybride|t[ée]l[ée]travail|\d\s*(jours?|days?)\s+(de\s+)?(remote|t[ée]l[ée]travail|from home|at home)|work from home|home office", text, re.I):
        return "Hybrid"
    if re.search(r"\bon[\s-]site\b|\bsur site\b|pr[ée]sentiel", text, re.I):
        return "On-site"
    return ""


def contract_of(c, text):
    ex = c.get("extra", {})
    if ex.get("contract"):
        return ex["contract"]
    m = re.search(r"\b(CDI|CDD|freelance|ind[ée]pendant|int[ée]rim|portage salarial)\b", text, re.I)
    if m:
        w = m.group(1).lower()
        return "CDI" if w == "cdi" else "CDD" if w == "cdd" else "Interim" if w.startswith("int") else "Freelance"
    for k in [ex.get("employment")] + list(ex.get("criteria") or []):
        hit = EMPLOYMENT.get(re.sub(r"[\s-]", "", str(k or "")).lower())
        if hit:
            return hit
    return ""


def details(c):
    """Everything /jobs highlights, derived from what collect.py already holds on a candidate.
    Pure: reads c, changes nothing. Missing data gives '' or [], never a guess."""
    desc = re.sub(r"\s+", " ", c.get("description") or "").strip()
    text = (c.get("title") or "") + "\n" + desc
    ex = c.get("extra") or {}
    label, k = salary_of(ex.get("salary") or "") if ex.get("salary") else ("", None)
    if not label:
        label, k = salary_of(text)
    if not label and ex.get("salary"):
        label = str(ex["salary"])[:60]
    seniority = ""
    for crit in ex.get("criteria") or []:
        seniority = SENIORITY.get(str(crit).strip().lower(), "") or seniority
    lang = c.get("lang")
    lang = "" if not lang else "EN" if lang == "en" else "FR+EN" if c.get("wants_english") else "FR"
    flags = [s for s in c.get("senior_soft") or []]
    flags += [name for name, rx in FLAGS_RX if rx.search(text)]
    if seniority in ("Mid-Senior", "Director", "Executive"):
        flags.append(seniority + " level")
    return {"salary": label, "salary_k": k, "years": years_of(text), "seniority": seniority,
            "junior": list(c.get("junior_evidence") or []), "work_mode": work_mode_of(c, text),
            "contract_found": contract_of(c, text), "lang_found": lang,
            "funnel": c.get("funnel") or "", "bucket": c.get("bucket") or "", "posted_label": str(ex.get("posted_label") or "")[:40],
            "excerpt": desc[:600], "skills": [name for name, rx in STACK_RX if rx.search(text)],
            "flags": flags}


def row(c, tier, fit, v=None):
    v = v or {}
    a = c.get("applicants")
    d = details(c)
    contract = v.get("contract") if v.get("contract") not in (None, "", "not stated") else d.pop("contract_found")
    d.pop("contract_found", None)
    lang = v.get("lang") or d["lang_found"]
    d.pop("lang_found")
    return {"key": key(c), "url": c.get("url") or "", "title": c.get("title") or "", "company": c.get("company") or "",
            "location": c.get("location") or "", "region": c.get("region") or "", "lang": lang,
            "contract": contract, "tier": tier, "fit": fit, "level": v.get("level") or "",
            "source": c.get("source") or "", "posted": c.get("posted") or "",
            "applicants": a if isinstance(a, int) else None, **d}


def main():
    out_dir = sys.argv[1]
    if not remote.enabled():
        sys.exit("HARVEST_API and HARVEST_TOKEN must be set")
    data = json.load(open(os.path.join(out_dir, "candidates.json"), encoding="utf-8"))
    verdicts = {}
    for path in sorted(glob.glob(os.path.join(out_dir, "verdicts*.jsonl"))):
        for line in open(path, encoding="utf-8"):
            try:
                v = json.loads(line)
                verdicts[v["id"]] = v
            except (ValueError, KeyError):
                continue
    # The morning apply order (harvest/MORNING.md): {"id", "rank", "plan"} per line, 1 = apply first.
    ranks = {}
    rpath = os.path.join(out_dir, "ranks.jsonl")
    if os.path.exists(rpath):
        for line in open(rpath, encoding="utf-8"):
            try:
                r = json.loads(line)
                if isinstance(r.get("rank"), int):
                    ranks[r["id"]] = r
            except (ValueError, KeyError):
                continue
    day = data["summary"]["date"]
    rows = []
    for c in data["candidates"]:
        v = verdicts.get(c["id"])
        if v is None:
            rows.append(row(c, "unjudged", "not judged yet | " + appl_str(c)))
            continue
        fit = v.get("fit", "")
        if v.get("tier") == "skip" and v.get("kill"):
            fit = "SKIP: %s. %s" % (v["kill"], fit)
        rows.append(row(c, v.get("tier", "unjudged"), "%s | %s" % (fit, appl_str(c)), v))
        r = ranks.get(c["id"])
        if r:
            rows[-1].update(rank=r["rank"], rank_day=day, plan=str(r.get("plan") or "")[:300])
    for k in data["killed"]:
        if str(k.get("why") or "").startswith(FETCHED_KILLS):
            rows.append(row(k, "killed", k["why"]))
    n = remote.push(day, rows)
    tiers = {}
    for r in rows:
        tiers[r["tier"]] = tiers.get(r["tier"], 0) + 1
    print("pushed %d rows for %s: %s; %d ranked" % (n, day, ", ".join("%s %d" % kv for kv in sorted(tiers.items())), len(ranks)))


if __name__ == "__main__":
    main()
