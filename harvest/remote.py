"""The Cloudflare Worker as the sweep's tracker, for runs away from the laptop (GitHub Actions).

    HARVEST_API    the Worker's address, e.g. https://hirehunt.iprabhattyagi.workers.dev
    HARVEST_TOKEN  the Worker's HARVEST_TOKEN secret (wrangler secret put HARVEST_TOKEN)

Both unset: every function here is a no-op, and a sweep on the laptop runs as before.
Stdlib only, so the Action needs nothing beyond requirements.txt.
"""
import json
import os
import urllib.request

# On the laptop the two values live in the repo's .env (README); the Action sets them as env vars.
# python-dotenv is in requirements.txt; a real env var always wins over the file.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except ImportError:
    pass

# Stripped: a secret pasted with a trailing space or newline would otherwise never match.
API = os.environ.get("HARVEST_API", "").strip().rstrip("/")
TOKEN = os.environ.get("HARVEST_TOKEN", "").strip()
BATCH = 400  # the Worker takes 500 rows a request at most


def enabled():
    return bool(API and TOKEN)


def _call(method, path, body=None):
    req = urllib.request.Request(
        API + path, method=method,
        data=None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"authorization": "Bearer " + TOKEN, "content-type": "application/json",
                 "user-agent": "cv-job-search-harvest"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def seen():
    """Every posting and application the Worker holds, as (url, company, title) triples."""
    if not enabled():
        return []
    d = _call("GET", "/api/harvest/seen")
    return [(r.get("url") or "", r.get("company") or "", r.get("title") or "")
            for r in d["postings"] + d["applications"]]


def push(day, rows):
    """Upsert one day's postings, in batches. Returns how many the Worker took."""
    n = 0
    for i in range(0, len(rows), BATCH):
        n += _call("POST", "/api/harvest/postings", {"day": day, "rows": rows[i:i + BATCH]})["upserted"]
    return n


def hunt(picks):
    """Set the Top 10 tab: [{id, rank, plan}], ids from pull(). Returns {"picked", "at"}."""
    return _call("POST", "/api/harvest/hunt", {"picks": picks})


def pull(since):
    """Postings the Worker has seen on or after `since` (YYYY-MM-DD), killed rows left out."""
    return _call("GET", "/api/harvest/postings?since=" + since)
