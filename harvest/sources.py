"""Source clients. Every function returns a list of raw candidate dicts with the same keys:

    id, source, title, company, location, posted, url, applicants, description, extra

`posted` is an ISO date when the source gives one, else the raw label. `applicants` is an int,
or -1 for "under N" bands, or None when unknown. Detail fetching (LinkedIn view page, HelloWork
offer page, ...) lives in `verify()` because it is the expensive half and must run only on rows
that survived the cheap filters.

Sources that are confirmed dead from this machine and from the doctrine (Indeed 403, Glassdoor
403, Welcome to the Jungle 403/302, Cadremploi, Monster, JobTeaser) are deliberately absent.
Stdlib only: `requests` is not installed and does not need to be.
"""
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}
DELAY = 1.2  # seconds between calls to the same host; LinkedIn throttles bursts


# A throttled host answers 429/503, and a dropped connection surfaces as -1. Both are temporary,
# and without a backoff the whole rest of a two-hour sweep silently returns zero rows: the 17 Sep
# run lost three lanes and every target board that way.
RETRY = (429, 500, 502, 503, 504, -1)


def get(url, data=None, headers=None, timeout=10, tries=4):
    h = dict(UA)
    h.update(headers or {})
    wait = 4.0
    status = -1
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace"), r.geturl()
        except urllib.error.HTTPError as e:
            status = e.code
        except Exception:
            status = -1
        if status not in RETRY or attempt == tries - 1:
            break
        time.sleep(wait)
        wait *= 2
    return status, "", url


def strip_tags(s):
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>|</li>|</div>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def row(**kw):
    base = {"id": None, "source": None, "title": "", "company": "", "location": "", "posted": "",
            "url": "", "applicants": None, "description": "", "extra": {}}
    base.update(kw)
    return base


# --------------------------------------------------------------------------- LinkedIn
LI_SEARCH = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
             "?keywords=%s&location=%s&f_TPR=r%d&start=%d")


def linkedin_search(keywords, location, days=7, pages=2):
    """Guest API. Real job IDs, an ISO posted date per card, and the mint band for free."""
    out, seen = [], set()
    for start in range(0, pages * 10, 10):
        url = LI_SEARCH % (urllib.parse.quote(keywords), urllib.parse.quote(location), int(days * 86400), start)
        status, body, _ = get(url)
        if status != 200 or not body.strip():
            break
        cards = body.split("<li>")[1:] or body.split("<li ")[1:]
        added = 0
        for card in cards:
            m = re.search(r"urn:li:jobPosting:(\d+)", card)
            if not m or m.group(1) in seen:
                continue
            jid = m.group(1)
            seen.add(jid)
            title = re.search(r'base-search-card__title[^>]*>\s*(.*?)\s*</h3>', card, re.S)
            company = re.search(r'base-search-card__subtitle[^>]*>.*?>\s*(.*?)\s*</a>', card, re.S) \
                or re.search(r'base-search-card__subtitle[^>]*>\s*(.*?)\s*</h4>', card, re.S)
            loc = re.search(r'job-search-card__location[^>]*>\s*(.*?)\s*</span>', card, re.S)
            date = re.search(r'datetime="([\d-]+)"', card)
            out.append(row(id="li:" + jid, source="LinkedIn", url="https://fr.linkedin.com/jobs/view/" + jid,
                           title=strip_tags(title.group(1)) if title else "",
                           company=strip_tags(company.group(1)) if company else "",
                           location=strip_tags(loc.group(1)) if loc else "",
                           posted=date.group(1) if date else "", extra={"li_id": int(jid)}))
            added += 1
        if added == 0:
            break
        time.sleep(DELAY)
    return out


def linkedin_detail(url):
    """View page: proves identity (company AND title), liveness, applicant count, and yields the
    description the judge needs. Rule 11: IDs get recycled, so identity is never assumed."""
    status, body, final = get(url)
    if status != 200:
        return {"alive": False, "status": status}
    low = body.lower()
    closed = ("no longer accepting applications" in low) or ("n'accepte plus de candidatures" in low) \
        or ("cette offre n'est plus disponible" in low)
    title = re.search(r'top-card-layout__title[^>]*>\s*(.*?)\s*</h1>', body, re.S)
    company = re.search(r'topcard__org-name-link[^>]*>\s*(.*?)\s*</a>', body, re.S) \
        or re.search(r'topcard__flavor[^>]*>\s*(.*?)\s*</span>', body, re.S)
    loc = re.search(r'topcard__flavor--bullet[^>]*>\s*(.*?)\s*</span>', body, re.S)
    posted = re.search(r'posted-time-ago__text[^>]*>\s*(.*?)\s*</span>', body, re.S)
    appl = None
    m = re.search(r'(\d[\d\s ]*)\s*(?:candidats|applicants)', body)
    if m:
        appl = int(re.sub(r"\D", "", m.group(1)) or 0)
    elif re.search(r"(parmi les|be among the first|soyez parmi les)\s*(\d+)", low):
        appl = -1
    desc = re.search(r'show-more-less-html__markup[^>]*>(.*?)</div>', body, re.S)
    crit = re.findall(r'description__job-criteria-text[^>]*>\s*(.*?)\s*</span>', body, re.S)
    return {"alive": not closed, "closed": closed, "status": status,
            "title": strip_tags(title.group(1)) if title else "",
            "company": strip_tags(company.group(1)) if company else "",
            "location": strip_tags(loc.group(1)) if loc else "",
            "posted_label": strip_tags(posted.group(1)) if posted else "",
            "applicants": appl,
            "description": strip_tags(desc.group(1))[:6000] if desc else "",
            "criteria": [strip_tags(c) for c in crit]}


# --------------------------------------------------------------------------- ATS APIs
def ashby(slug, company):
    status, body, _ = get("https://api.ashbyhq.com/posting-api/job-board/%s" % slug)
    if status != 200:
        return []
    out = []
    for j in json.loads(body).get("jobs", []):
        if not j.get("isListed", True):
            continue
        out.append(row(id="ashby:%s:%s" % (slug, j.get("id")), source="Ashby (own ATS)", company=company,
                       title=j.get("title", ""), location=j.get("location", ""),
                       posted=(j.get("publishedAt") or "")[:10], url=j.get("jobUrl", ""),
                       description=strip_tags(j.get("descriptionHtml", "") or "")[:6000],
                       extra={"employment": j.get("employmentType", ""), "remote": j.get("isRemote")}))
    return out


def greenhouse(slug, company):
    status, body, _ = get("https://boards-api.greenhouse.io/v1/boards/%s/jobs?content=true" % slug)
    if status != 200:
        return []
    out = []
    for j in json.loads(body).get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        out.append(row(id="gh:%s:%s" % (slug, j.get("id")), source="Greenhouse (own ATS)", company=company,
                       title=j.get("title", ""), location=loc, posted=(j.get("updated_at") or "")[:10],
                       url=j.get("absolute_url", ""),
                       description=strip_tags(html.unescape(j.get("content", "") or ""))[:6000]))
    return out


def lever(slug, company):
    status, body, _ = get("https://api.lever.co/v0/postings/%s?mode=json" % slug)
    if status != 200:
        return []
    out = []
    for j in json.loads(body):
        ts = j.get("createdAt")
        posted = time.strftime("%Y-%m-%d", time.gmtime(ts / 1000)) if ts else ""
        out.append(row(id="lever:%s:%s" % (slug, j.get("id")), source="Lever (own ATS)", company=company,
                       title=j.get("text", ""), location=(j.get("categories") or {}).get("location", ""),
                       posted=posted, url=j.get("hostedUrl", ""),
                       description=(j.get("descriptionPlain", "") or "")[:6000]))
    return out


def smartrecruiters(slug, company):
    status, body, _ = get("https://api.smartrecruiters.com/v1/companies/%s/postings?limit=100" % slug)
    if status != 200:
        return []
    out = []
    for j in json.loads(body).get("content", []):
        loc = j.get("location") or {}
        out.append(row(id="sr:%s:%s" % (slug, j.get("id")), source="SmartRecruiters (own ATS)", company=company,
                       title=j.get("name", ""), location=", ".join(x for x in (loc.get("city"), loc.get("country")) if x),
                       posted=(j.get("releasedDate") or "")[:10],
                       url="https://jobs.smartrecruiters.com/%s/%s" % (slug, j.get("id")),
                       extra={"detail_api": "https://api.smartrecruiters.com/v1/companies/%s/postings/%s" % (slug, j.get("id"))}))
    return out


def smartrecruiters_detail(api_url):
    status, body, _ = get(api_url)
    if status != 200:
        return ""
    sections = (json.loads(body).get("jobAd") or {}).get("sections") or {}
    return strip_tags(" ".join((s or {}).get("text", "") for s in sections.values()))[:6000]


ATS = {"ashby": ashby, "greenhouse": greenhouse, "lever": lever, "smartrecruiters": smartrecruiters}


# --------------------------------------------------------------------------- Amazon.jobs
def amazon_jobs(query, location="France"):
    url = ("https://www.amazon.jobs/en/search.json?base_query=%s&loc_query=%s&result_limit=100&offset=0"
           % (urllib.parse.quote(query), urllib.parse.quote(location)))
    status, body, _ = get(url)
    if status != 200:
        return []
    out = []
    for j in json.loads(body).get("jobs", []):
        out.append(row(id="amz:%s" % j.get("id_icims", j.get("id")), source="amazon.jobs (own ATS)",
                       company="Amazon", title=j.get("title", ""), location=j.get("location", ""),
                       posted=j.get("posted_date", ""), url="https://www.amazon.jobs" + (j.get("job_path") or ""),
                       description=strip_tags(j.get("description", "") or "")[:6000]))
    return out


# --------------------------------------------------------------------------- HelloWork
def hellowork_search(keywords, location):
    """Search cards carry title="<Title> - <Company>" on the offer anchor and a localisationCard
    div; nothing else on the page is trusted. The offer page (fetched in verify) carries the real
    absolute date in JSON-LD, which is why HelloWork ranked above LinkedIn on freshness."""
    url = "https://www.hellowork.com/fr-fr/emploi/recherche.html?k=%s&l=%s" % (
        urllib.parse.quote_plus(keywords), urllib.parse.quote_plus(location))
    status, body, _ = get(url)
    if status != 200:
        return []
    out, seen = [], set()
    for card in body.split('data-cy="serpCard"')[1:]:
        m = re.search(r'href="/fr-fr/emplois/(\d+)\.html"[^>]*?title="([^"]*)"', card, re.S)
        if not m or m.group(1) in seen:
            continue
        oid = m.group(1)
        seen.add(oid)
        title_company = html.unescape(m.group(2))
        title, _, company = title_company.rpartition(" - ")
        if not title:
            title, company = title_company, ""
        loc = re.search(r'data-cy="localisationCard"[^>]*>\s*(.*?)\s*</div>', card, re.S)
        out.append(row(id="hw:" + oid, source="HelloWork", title=title.strip(), company=company.strip(),
                       location=strip_tags(loc.group(1)) if loc else "",
                       url="https://www.hellowork.com/fr-fr/emplois/%s.html" % oid, extra={"hw_id": int(oid)}))
    return out


def _jsonld(body, key):
    m = re.search(r'"%s"\s*:\s*("(?:[^"\\]|\\.)*"|\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\})' % key, body)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


def hellowork_detail(url):
    status, body, _ = get(url)
    if status != 200:
        return {"alive": False, "status": status}
    low = body.lower()
    closed = "offre expir" in low or "n'est plus disponible" in low or "offre pourvue" in low \
        or "cette offre a expir" in low
    title = re.search(r'data-cy="jobTitle"[^>]*>\s*(.*?)\s*</span>', body, re.S) \
        or re.search(r"<h1[^>]*>(.*?)</h1>", body, re.S)
    org = _jsonld(body, "hiringOrganization") or {}
    locd = _jsonld(body, "jobLocation") or {}
    addr = locd.get("address", {}) if isinstance(locd, dict) else {}
    location = ", ".join(x for x in (addr.get("addressLocality"), addr.get("postalCode"), addr.get("addressRegion")) if x)
    posted = _jsonld(body, "datePosted") or ""
    desc = _jsonld(body, "description") or ""
    if not desc:
        d = re.search(r'data-cy="jobDescription"[^>]*>(.*?)</section>', body, re.S)
        desc = d.group(1) if d else ""
    return {"alive": not closed, "closed": closed, "status": status,
            "title": strip_tags(title.group(1)) if title else "",
            "company": (org.get("name") if isinstance(org, dict) else "") or "",
            "location": location,
            "posted_iso": posted[:10] if isinstance(posted, str) else "",
            "posted_label": posted[:10] if isinstance(posted, str) else "",
            "applicants": None,
            "description": strip_tags(desc)[:6000]}


# --------------------------------------------------------------------------- France Travail
def francetravail_search(keywords, lieu="75D"):
    url = "https://candidat.francetravail.fr/offres/recherche?motsCles=%s&lieux=%s&tri=1" % (
        urllib.parse.quote_plus(keywords), lieu)
    status, body, _ = get(url)
    if status != 200:
        return []
    out, seen = [], set()
    for m in re.finditer(r'href="(/offres/recherche/detail/(\w+))"[^>]*>(.*?)</a>', body, re.S):
        oid = m.group(2)
        if oid in seen:
            continue
        seen.add(oid)
        # The card is newline-separated: title / company / "92 - Plessis-Robinson" / snippet.
        # Taking the whole blob as the title made every row fail the identity check in verify()
        # against the real page title, which silently binned the entire source.
        parts = [x.strip() for x in strip_tags(m.group(3)).split("\n") if x.strip()]
        if parts and parts[0].startswith("(déjà vu)"):
            parts = parts[1:] if parts[0] == "(déjà vu)" else [parts[0][9:].strip()] + parts[1:]
        title = parts[0] if parts else ""
        company = parts[1] if len(parts) > 1 else ""
        loc = parts[2].lstrip("\xa0- ").strip() if len(parts) > 2 else ""
        out.append(row(id="ft:" + oid, source="France Travail", title=title[:120],
                       company=company[:80], location=loc[:80],
                       url="https://candidat.francetravail.fr" + m.group(1)))
    return out


def francetravail_detail(url):
    status, body, _ = get(url)
    if status != 200:
        return {"alive": False, "status": status}
    title = re.search(r"<h1[^>]*>(.*?)</h1>", body, re.S)
    company = re.search(r'class="[^"]*t4 title[^"]*"[^>]*>(.*?)</', body, re.S) \
        or re.search(r"Entreprise\s*:?\s*</[^>]+>\s*<[^>]+>(.*?)</", body, re.S)
    loc = re.search(r'itemprop="addressLocality"[^>]*>(.*?)</', body, re.S) \
        or re.search(r"<span[^>]*>\s*(\d{2}\s*-\s*[A-ZÉ][^<]{2,40})</span>", body)
    date = re.search(r'itemprop="datePosted"[^>]*content="([\d-]+)"', body) \
        or re.search(r"Publi[ée]e? le\s*(\d{1,2}\s+\w+\s+\d{4})", body)
    desc = re.search(r'itemprop="description"[^>]*>(.*?)</div>', body, re.S)
    return {"alive": True, "closed": False, "status": status,
            "title": re.sub(r"^Offre\s*n[°o]\s*\w+\s*", "", strip_tags(title.group(1)).strip()) if title else "",
            "company": strip_tags(company.group(1)) if company else "",
            "location": strip_tags(loc.group(1)) if loc else "",
            "posted_label": date.group(1) if date else "",
            "applicants": None,
            "description": strip_tags(desc.group(1))[:6000] if desc else ""}


# --------------------------------------------------------------------------- APEC
def apec_search(keywords, lieux, range_=30):
    """Discovery only: every APEC detail route is behind DataDome, so these rows can never be
    verified and never hold a tier above 'verify'. APEC's own low-applicant flag is kept."""
    body = json.dumps({"motsCles": keywords, "lieux": lieux, "typesContrat": [101888, 101887],
                       "pagination": {"startIndex": 0, "range": range_},
                       "sorts": [{"type": "DATE", "direction": "DESCENDING"}], "activeFiltre": True}).encode()
    status, text, _ = get("https://www.apec.fr/cms/webservices/rechercheOffre", data=body,
                          headers={"Content-Type": "application/json"})
    if status != 200:
        return []
    out = []
    for j in json.loads(text).get("resultats", []):
        oid = j.get("numeroOffre")
        out.append(row(id="apec:%s" % oid, source="APEC", title=j.get("intitule", ""),
                       company=j.get("nomCommercial", ""), location=j.get("lieuTexte", ""),
                       posted=(j.get("datePublication") or "")[:10],
                       url="https://www.apec.fr/candidat/recherche-emploi.html/emploi/detail-offre/%s" % oid,
                       applicants=(-1 if j.get("indicateurFaibleCandidature") else None),
                       description=strip_tags(j.get("texteOffre", "") or "")[:6000],
                       extra={"salary": j.get("salaireTexte", ""),
                              "contract": {101888: "CDI", 101887: "CDD"}.get(j.get("typeContrat"), ""),
                              "low_applicants": bool(j.get("indicateurFaibleCandidature"))}))
    return out


DETAIL = {"LinkedIn": linkedin_detail, "HelloWork": hellowork_detail, "France Travail": francetravail_detail}
