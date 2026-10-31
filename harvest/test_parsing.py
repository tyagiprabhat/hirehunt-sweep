"""Smallest checks that fail if the two parsers regress. Run: python harvest/test_parsing.py"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import collect  # noqa: E402
import sources  # noqa: E402

# France Travail cards arrive as one newline-separated blob. Taking the whole blob as the title
# made every row fail verify()'s identity check against the real page title, which silently
# binned the source: 303 of 530 rows on 2026-09-28.
CARD = '(déjà vu) \n Supply Planner H/F \nMichael Page\n\xa0-\xa0 92 - Plessis-Robinson \n POSTE : Supply Pl'
parts = [x.strip() for x in CARD.split("\n") if x.strip()]
if parts[0].startswith("(déjà vu)"):
    parts = parts[1:] if parts[0] == "(déjà vu)" else [parts[0][9:].strip()] + parts[1:]
assert parts[0] == "Supply Planner H/F", parts[0]
assert parts[1] == "Michael Page", parts[1]
assert parts[2].lstrip("\xa0- ").strip() == "92 - Plessis-Robinson", parts[2]

# The detail <h1> carries an offer-number prefix no card title has, so identity must survive it.
strip = lambda t: re.sub(r"^Offre\s*n[°o]\s*\w+\s*", "", t.strip())  # noqa: E731
assert strip("Offre n° 7332214 Supply Planner H/F") == "Supply Planner H/F"
assert collect.norm_title("Supply Planner H/F")[:10] == \
       collect.norm_title(strip("Offre n° 7332214 Supply Planner H/F"))[:10]

# Language buckets: a posting is filed by the language it is written in, not by the phrase
# that found it, and a French posting only earns a bucket when it also asks for English.
assert collect.post_lang("Nous recherchons un analyste, vous serez en charge des missions.") == "fr"
assert collect.post_lang("We are looking for an analyst to join our team, you will own it.") == "en"
assert collect.EN_REQUIRED.search("Vous avez un bon niveau d'anglais courant.")
assert not collect.EN_REQUIRED.search("Poste basé à Paris, équipe francophone.")

# Region: bare IDF suburb names used to fall through to "Unknown", and "Town, France" to "ex-IDF",
# so the row never reached the IDF sheet (169 of 801 verified rows on 2026-09-30).
for loc, want in {"Drancy": "IDF", "Drancy, France": "IDF", "Fontenay-sous-Bois": "IDF",
                  "Villejuif": "IDF", "Le Kremlin-Bicêtre": "IDF", "St-Denis": "IDF",
                  "Torcy - 77": "IDF", "Paris 01 - 75": "IDF", "Lyon 01 - 69": "ex-IDF",
                  "Bordeaux, France": "ex-IDF", "New York, New York, USA": "Abroad"}.items():
    assert collect.region_of(loc) == want, (loc, collect.region_of(loc), want)

# Tier folding: four harvest eras wrote ~50 spellings of four grades.
import master  # noqa: E402
for raw, want in {"1-strong": "1-strong", "P2": "2-solid", "B1": "2-solid", "A2": "1-strong",
                  "Medium": "2-solid", "Low": "2-stretch", "3-maybe": "2-stretch", "Top": "1-strong",
                  "skip - too senior (manager)": "skip", "killed": "skip", "dead": "skip",
                  "language-gated": "skip", "2 - review": "", "unverified": "", None: "",
                  "1-good": "1-strong", "D3": "skip"}.items():
    assert master.tier_of(raw) == want, (raw, master.tier_of(raw), want)
assert master.lang_of("FR posting; English required") == "FR+EN"
assert master.lang_of("EN/FR") == "FR+EN" and master.lang_of("French posting") == "FR"
assert master.lang_of(None) == ""

# /jobs details: derived from what the collector already holds, never guessed.
import push  # noqa: E402
for text, want in {"Salaire : 45 000 € - 55 000 € brut annuel": ("45-55k EUR", 45), "45k-55k€": ("45-55k EUR", 45),
                   "entre 40 et 45 k€": ("40-45k EUR", 40), "€45,000 - €52,000 per year": ("45-52k EUR", 45),
                   "Rémunération 50k€ + variable": ("50k EUR", 50), "TJM 500€/jour": ("", None),
                   "salaire 2 100 € par mois": ("", None), "We are 300 people in 12 countries": ("", None)}.items():
    assert push.salary_of(text) == want, (text, push.salary_of(text), want)
for text, want in {"Vous avez 2 à 3 ans d'expérience": "2-3 yrs", "3+ years of experience in FP&A": "3+ yrs",
                   "Expérience : 1 an minimum": "1+ yrs", "a company founded 20 years ago": ""}.items():
    assert push.years_of(text) == want, (text, push.years_of(text), want)
c = {"title": "Supply Planner H/F", "company": "Acme", "location": "Paris (Hybrid)", "region": "IDF",
     "description": "Nous recherchons un planificateur. CDI. Maîtrise d'Excel et SAP, Power BI apprécié. "
                    "Anglais courant. 2 à 3 ans d'expérience. Permis B.",
     "lang": "fr", "wants_english": True, "funnel": "quiet", "applicants": -1, "senior_soft": [],
     "junior_evidence": ["2 à 3 ans"], "extra": {"posted_label": "2 days ago", "criteria": ["Entry level", "Full-time"]}}
d = push.details(c)
assert d["skills"] == ["Excel", "Power BI", "SAP"], d["skills"]
assert (d["work_mode"], d["contract_found"], d["lang_found"], d["years"], d["seniority"]) == \
       ("Hybrid", "CDI", "FR+EN", "2-3 yrs", "Entry level"), d
assert d["flags"] == ["driving licence"] and d["salary"] == "" and d["salary_k"] is None, d
assert push.details({"title": "x"})["excerpt"] == ""   # an old candidate with nothing still pushes
r = push.row(c, "unjudged", "not judged yet", {"contract": "not stated", "lang": "FR"})
assert (r["contract"], r["lang"], r["funnel"]) == ("CDI", "FR", "quiet") and "contract_found" not in r, r

print("ok: france travail card, detail title, language buckets, IDF communes, tiers, jobs details")
