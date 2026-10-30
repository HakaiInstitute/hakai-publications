"""Gather new publications by Hakai-affiliated authors from OpenAlex, Crossref
and DataCite, and append them to publications.json.

A work is added when at least one of its authors lists the Hakai Institute
(ROR 02pry0c91, or "Hakai" in the affiliation text) on that work. It is found
either through an ORCID in hakai_authors.csv or through a Hakai affiliation
search. Existing entries are never edited or removed, so hand edits and
"rejected" statuses are kept and rejected works are not re-added.

New entries get status "published" on the first run (the initial backfill) and
"needs review" on every later run. Each entry has a plain-text citation in an
APA 7 style: up to 10 authors in full, otherwise "First, A., et al.".

Usage:
    python gather_publications.py [--dry-run] [--full] [--lookback-days N]
                                  [--status STATUS] [--report report.md]
                                  [--authors hakai_authors.csv]
                                  [--publications publications.json]
                                  [--output publications.json]

Optional environment variables:
    OPENALEX_API_KEY   free key from https://openalex.org/settings/api
    GATHER_MAILTO      contact address for the OpenAlex/Crossref polite pools
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

HAKAI_ROR = "02pry0c91"
ROR_URL = f"https://ror.org/{HAKAI_ROR}"
OPENALEX_KEY = os.environ.get("OPENALEX_API_KEY")
MAILTO = os.environ.get("GATHER_MAILTO")
SOURCE_ORDER = ["OpenAlex", "Crossref", "DataCite"]  # priority when merging fields

OPENALEX_TYPES = {
    "article": "article",
    "review": "article",
    "letter": "article",
    "editorial": "article",
    "preprint": "preprint",
    "book": "book",
    "book-chapter": "book-chapter",
    "dissertation": "thesis",
    "report": "report",
}
CROSSREF_TYPES = {
    "journal-article": "article",
    "posted-content": "preprint",
    "book-chapter": "book-chapter",
    "book": "book",
    "monograph": "book",
    "edited-book": "book",
    "dissertation": "thesis",
    "proceedings-article": "conference-paper",
    "report": "report",
}
DATACITE_TYPES = {
    "Text": "article",
    "JournalArticle": "article",
    "Preprint": "preprint",
    "Dissertation": "thesis",
    "Book": "book",
    "BookChapter": "book-chapter",
    "ConferencePaper": "conference-paper",
    "ConferenceProceeding": "conference-paper",
    "Report": "report",
}
PROTOCOL_PREFIXES = ("10.17504/",)  # protocols.io
EXCLUDED_PREFIXES = ("10.48321/",)  # DMPTool data management plans
NAME_PARTICLES = {"van", "von", "de", "der", "den", "del", "della", "di", "da", "du", "la", "le", "ter", "bin", "al"}


def warn(msg):
    print(f"::warning::{msg}" if os.environ.get("GITHUB_ACTIONS") else f"WARNING: {msg}", file=sys.stderr)


# ---- http ------------------------------------------------------------------
_last_call = {}


def http_json(url, params=None, retries=4, min_interval=0.25):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(
            params, quote_via=urllib.parse.quote, safe=":/,|!"
        )
    host = urllib.parse.urlparse(url).netloc
    ua = "hakai-publications-gatherer" + (f" (mailto:{MAILTO})" if MAILTO else "")
    for attempt in range(retries):
        wait = min_interval - (time.time() - _last_call.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        _last_call[host] = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            body = e.read().decode("utf-8", "ignore")[:300]
            raise RuntimeError(f"HTTP {e.code} from {host}: {body}") from e
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            raise


# ---- normalisation helpers -------------------------------------------------
def norm_doi(x):
    if not x:
        return None
    x = re.sub(r"^https?://(dx\.)?doi\.org/", "", x.strip(), flags=re.I).lower()
    return x or None


def norm_orcid(x):
    m = re.search(r"\d{4}-\d{4}-\d{4}-\d{3}[\dXx]", x or "")
    return m.group(0).upper() if m else None


def ascii_key(s):
    s = unicodedata.normalize("NFKD", s or "")
    return re.sub(r"[^a-z]", "", s.encode("ascii", "ignore").decode().lower())


def title_key(t):
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower())


def is_hakai(*texts):
    blob = " ".join(t for t in texts if t)
    return HAKAI_ROR in blob or "hakai" in blob.lower()


def split_display_name(name):
    """'Matthew Lemay' -> ('Lemay', 'Matthew'), keeping particles like 'van der'."""
    toks = (name or "").split()
    if not toks:
        return "", ""
    i = len(toks) - 1
    while i > 0 and toks[i - 1].lower() in NAME_PARTICLES:
        i -= 1
    return " ".join(toks[i:]), " ".join(toks[:i])


def initials(given):
    out = []
    for tok in (given or "").replace(".", " ").split():
        out.append("-".join(p[0].upper() + "." for p in tok.split("-") if p))
    return " ".join(out)


def fmt_name(family, given):
    ini = initials(given)
    return f"{family}, {ini}" if ini else family


# ---- author list -----------------------------------------------------------
def load_authors(path):
    """Return (orcid -> name, [name-only people]); duplicate ORCID rows are merged."""
    by_orcid, name_only = {}, []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            name = (row.get("author") or "").strip()
            if not name:
                continue
            if "," in name:
                family, given = [p.strip() for p in name.split(",", 1)]
            else:
                family, given = split_display_name(name)
            orcid = norm_orcid(row.get("orcid"))
            person = {"name": name, "family": family, "given": given, "orcid": orcid}
            if orcid:
                by_orcid.setdefault(orcid, person)
            else:
                name_only.append(person)
    return by_orcid, name_only


def name_matches(family, given, person):
    a, b = ascii_key(family), ascii_key(person["family"])
    if not a or not b or not (a == b or b.endswith(a) or a.endswith(b)):
        return False
    return ascii_key(given)[:1] == ascii_key(person["given"])[:1]


# ---- source parsers --------------------------------------------------------
def make_work(source, doi, title, year, wtype, venue, volume, issue, pages, authors, **extra):
    return {
        "doi": doi,
        "title": (title or "").strip(),
        "year": year,
        "type": wtype,
        "venue": venue,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "authors": authors,
        "sources": {source},
        "related_dois": set(),
        "openalex_id": None,
        "is_version": False,
        **extra,
    }


def parse_openalex(w):
    wtype = OPENALEX_TYPES.get(w.get("type"))
    doi = norm_doi(w.get("doi"))
    if doi and doi.startswith(PROTOCOL_PREFIXES):
        wtype = "protocol"
    if wtype is None:
        return None
    biblio = w.get("biblio") or {}
    first, last = biblio.get("first_page"), biblio.get("last_page")
    pages = first if first and (not last or last == first) else f"{first}-{last}" if first else None
    authors = []
    for a in w.get("authorships") or []:
        au = a.get("author") or {}
        strings = list(a.get("raw_affiliation_strings") or [])
        strings += [x.get("raw_affiliation_string") for x in a.get("affiliations") or []]
        strings += [i.get("display_name") for i in a.get("institutions") or []]
        strings += [i.get("ror") for i in a.get("institutions") or []]
        family, given = split_display_name(au.get("display_name"))
        authors.append(
            {"family": family, "given": given, "orcid": norm_orcid(au.get("orcid")), "hakai_affil": is_hakai(*[s for s in strings if s])}
        )
    src = ((w.get("primary_location") or {}).get("source")) or {}
    work = make_work(
        "OpenAlex", doi, w.get("display_name"), w.get("publication_year"), wtype,
        src.get("display_name"), biblio.get("volume"), biblio.get("issue"), pages, authors,
    )
    work["openalex_id"] = w.get("id")
    return work


def parse_crossref(w):
    wtype = CROSSREF_TYPES.get(w.get("type"))
    doi = norm_doi(w.get("DOI"))
    if doi and doi.startswith(PROTOCOL_PREFIXES):
        wtype = "protocol"
    if wtype is None:
        return None
    authors = []
    for a in w.get("author") or []:
        affils = [x.get("name") for x in a.get("affiliation") or []]
        authors.append(
            {
                "family": a.get("family") or a.get("name") or "",
                "given": a.get("given") or "",
                "orcid": norm_orcid(a.get("ORCID")),
                "hakai_affil": is_hakai(*[s for s in affils if s]),
            }
        )
    year = ((w.get("issued") or {}).get("date-parts") or [[None]])[0][0]
    venue = (w.get("container-title") or [None])[0] or w.get("publisher")
    work = make_work(
        "Crossref", doi, (w.get("title") or [""])[0], year, wtype, venue,
        w.get("volume"), w.get("issue"), w.get("page"), authors,
    )
    rel = w.get("relation") or {}
    for key in ("has-preprint", "is-preprint-of"):
        for r in rel.get(key) or []:
            if r.get("id-type") == "doi":
                work["related_dois"].add(norm_doi(r.get("id")))
    return work


def parse_datacite(d):
    a = d["attributes"]
    doi = norm_doi(a.get("doi"))
    types = a.get("types") or {}
    general, specific = types.get("resourceTypeGeneral"), (types.get("resourceType") or "")
    if re.search(r"plan", specific, re.I) or (doi and doi.startswith(EXCLUDED_PREFIXES)):
        return None
    wtype = DATACITE_TYPES.get(general)
    if re.search(r"protocol", specific, re.I) or (doi and doi.startswith(PROTOCOL_PREFIXES)):
        wtype = "protocol"
    if wtype is None:
        return None
    related = a.get("relatedIdentifiers") or []
    is_version = any(r.get("relationType") == "IsVersionOf" for r in related)
    authors = []
    for c in a.get("creators") or []:
        affils = []
        for x in c.get("affiliation") or []:
            affils += [x.get("name"), x.get("affiliationIdentifier")] if isinstance(x, dict) else [x]
        family, given = c.get("familyName"), c.get("givenName")
        if not family:
            name = c.get("name") or ""
            family, given = ([p.strip() for p in name.split(",", 1)] + [""])[:2] if "," in name else (name, "")
        orcid = next(
            (norm_orcid(n.get("nameIdentifier")) for n in c.get("nameIdentifiers") or [] if "orcid" in (n.get("nameIdentifierScheme") or "").lower()),
            None,
        )
        authors.append({"family": family, "given": given or "", "orcid": orcid, "hakai_affil": is_hakai(*[s for s in affils if s])})
    container = a.get("container") or {}
    publisher = a.get("publisher")
    publisher = publisher.get("name") if isinstance(publisher, dict) else publisher
    first, last = container.get("firstPage"), container.get("lastPage")
    titles = a.get("titles") or [{}]
    work = make_work(
        "DataCite", doi, titles[0].get("title"), a.get("publicationYear"), wtype,
        container.get("title") or publisher, container.get("volume"), container.get("issue"),
        f"{first}-{last}" if first and last else first, authors,
    )
    work["is_version"] = is_version
    for r in related:
        if r.get("relatedIdentifierType") == "DOI" and r.get("relationType") in ("IsPreprintOf", "HasPreprint", "IsVariantFormOf"):
            work["related_dois"].add(norm_doi(r.get("relatedIdentifier")))
    return work


# ---- source fetchers -------------------------------------------------------
def chunks(seq, n):
    seq = list(seq)
    return [seq[i : i + n] for i in range(0, len(seq), n)]


def fetch_openalex(orcids, since):
    base = ["from_publication_date:" + since] if since else []
    queries = [",".join(base + [f"authorships.institutions.ror:{ROR_URL}"])]
    queries += [",".join(base + ["authorships.author.orcid:" + "|".join(c)]) for c in chunks(orcids, 25)]
    seen, works = set(), []
    for flt in queries:
        # page-number paging (cursor paging needs an API key); fine below 10,000 results
        page = 1
        while True:
            params = {
                "filter": flt, "per-page": 200, "page": page,
                "select": "id,doi,display_name,publication_year,type,primary_location,biblio,authorships",
            }
            if OPENALEX_KEY:
                params["api_key"] = OPENALEX_KEY
            if MAILTO:
                params["mailto"] = MAILTO
            j = http_json("https://api.openalex.org/works", params)
            results = j.get("results") or []
            for r in results:
                if r["id"] not in seen:
                    seen.add(r["id"])
                    works.append(r)
            if len(results) < 200 or page * 200 >= 10000:
                break
            page += 1
    return [w for w in map(parse_openalex, works) if w]


def fetch_crossref(orcids, since):
    base = [f"from-pub-date:{since}"] if since else []
    select = "DOI,title,author,issued,type,container-title,publisher,volume,issue,page,relation"
    raw, seen = [], set()

    def pull(extra_filters, query=None, stop_when_no_hakai=False):
        cursor = "*"
        while cursor:
            params = {"rows": 200, "cursor": cursor, "select": select}
            if extra_filters or base:
                params["filter"] = ",".join(base + extra_filters)
            if query:
                params["query.affiliation"] = query
            if MAILTO:
                params["mailto"] = MAILTO
            m = http_json("https://api.crossref.org/works", params)["message"]
            items = m.get("items") or []
            hits = 0
            for it in items:
                hakai = any(is_hakai(*[x.get("name") for x in a.get("affiliation") or [] if x.get("name")]) for a in it.get("author") or [])
                hits += hakai
                if it["DOI"] not in seen and (hakai or not stop_when_no_hakai):
                    seen.add(it["DOI"])
                    raw.append(it)
            if not items or (stop_when_no_hakai and hits == 0):
                break  # affiliation search is ranked by relevance, so stop once it dries up
            cursor = m.get("next-cursor")

    for orcid in orcids:
        pull([f"orcid:{orcid}"])
    pull([], query="Hakai Institute", stop_when_no_hakai=True)
    return [w for w in map(parse_crossref, raw) if w]


def fetch_datacite(orcids, since):
    clauses = [f'creators.affiliation.affiliationIdentifier:"{ROR_URL}"', "creators.affiliation.name:Hakai"]
    clauses += [
        " OR ".join(f'creators.nameIdentifiers.nameIdentifier:"https://orcid.org/{o}"' for o in c) for c in chunks(orcids, 15)
    ]
    raw, seen = [], set()
    for q in clauses:
        if since:
            q = f"({q}) AND publicationYear:[{since[:4]} TO *]"
        url, params = "https://api.datacite.org/dois", {"query": q, "page[size]": 1000, "page[cursor]": 1, "affiliation": "true"}
        while url:
            j = http_json(url, params)
            for d in j.get("data") or []:
                if d["id"] not in seen:
                    seen.add(d["id"])
                    raw.append(d)
            url, params = (j.get("links") or {}).get("next") if j.get("data") else None, None
    return [w for w in map(parse_datacite, raw) if w and not w["is_version"]]


# ---- merge and filter ------------------------------------------------------
def work_key(w):
    return w["doi"] or f"t:{title_key(w['title'])}-{w['year']}"


def merge(works):
    merged = {}
    for w in sorted(works, key=lambda x: SOURCE_ORDER.index(next(iter(x["sources"])))):
        k = work_key(w)
        if k not in merged:
            merged[k] = w
            continue
        m = merged[k]
        m["sources"] |= w["sources"]
        m["related_dois"] |= w["related_dois"]
        for f in ("title", "year", "type", "venue", "volume", "issue", "pages", "openalex_id"):
            m[f] = m[f] or w[f]
        if not m["authors"]:
            m["authors"] = w["authors"]
        else:  # carry over Hakai flags and ORCIDs seen in other sources
            for a in m["authors"]:
                for b in w["authors"]:
                    if (
                        (a["orcid"] and a["orcid"] == b["orcid"])
                        or (
                            not (a["orcid"] and b["orcid"])
                            and ascii_key(a["family"]) == ascii_key(b["family"])
                            and ascii_key(a["given"])[:1] == ascii_key(b["given"])[:1]
                        )
                    ):
                        a["hakai_affil"] = a["hakai_affil"] or b["hakai_affil"]
                        a["orcid"] = a["orcid"] or b["orcid"]
    return list(merged.values())


def is_unwanted_zenodo(w):
    return bool(
        w["doi"]
        and w["doi"].startswith("10.5281/")
        and w["type"] not in ("thesis", "protocol")
        and not re.search(r"\b(?:protocols?|thesis|dissertations?)\b", w["title"], re.I)
    )


def annotate(w, by_orcid, name_only):
    """Set Hakai flags and match reasons; return True if the work should be kept."""
    reasons = []
    for a in w["authors"]:
        listed = by_orcid.get(a["orcid"])
        # only the affiliation on this work counts: a listed author who has since
        # left Hakai is not a Hakai author on a paper that doesn't list Hakai
        a["hakai"] = bool(a["hakai_affil"])
        if listed:
            reasons.append(f"ORCID: {listed['name']}")
        if a["hakai_affil"]:
            reasons.append(f"affiliation: {fmt_name(a['family'], a['given'])}")
        elif not listed:
            for p in name_only:
                if name_matches(a["family"], a["given"], p):
                    reasons.append(f"name only (unverified): {p['name']}")
    w["match_reasons"] = sorted(set(reasons))
    return any(a["hakai_affil"] for a in w["authors"])


# ---- citation (APA 7 style, shortened for long author lists) ----------------
MAX_CITED_AUTHORS = 10  # longer author lists are cited as "First, A., et al."


def join_names(names):
    """Author part of a citation from already formatted names ('Family, G.')."""
    if not names:
        return ""
    if len(names) > MAX_CITED_AUTHORS:
        return f"{names[0]}, et al."
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + ", & " + names[-1]


def apa_authors(authors):
    return join_names([fmt_name(a["family"], a["given"]) for a in authors if a["family"]])


def apa_citation(w):
    """Authors (Year). Title. Venue, volume(issue), pages. DOI URL, as plain text.
    Not strict APA 7: lists of more than MAX_CITED_AUTHORS authors become "First, A., et al."
    (APA 7 would list up to 20)."""
    auth = apa_authors(w["authors"])
    title = w["title"].rstrip()
    head = f"{auth} " if auth else ""
    cit = f"{head}({w['year'] or 'n.d.'}). {title}{'' if title[-1:] in '.?!' else '.'}"
    if w["venue"]:
        cit += f" {w['venue']}"
        if w["volume"]:
            cit += f", {w['volume']}"
            if w["issue"]:
                cit += f"({w['issue']})"
        if w["pages"]:
            cit += f", {w['pages']}"
        cit += "."
    if w["doi"]:
        cit += f" https://doi.org/{w['doi']}"
    return re.sub(r"\s+", " ", cit).strip()


def to_entry(w, today, status):
    entry = {
        "year": str(w["year"]) if w["year"] else "",
        "citation": apa_citation(w),
        "url": f"https://doi.org/{w['doi']}" if w["doi"] else "",
        "doi": w["doi"] or "",
        "status": status,
        "title": w["title"],
        "authors": [
            {"name": fmt_name(a["family"], a["given"]), **({"orcid": a["orcid"]} if a["orcid"] else {}), "hakai": a["hakai"]}
            for a in w["authors"]
        ],
        "hakai_authors": [fmt_name(a["family"], a["given"]) for a in w["authors"] if a["hakai"]],
        "venue": w["venue"] or "",
        "type": w["type"],
        "sources": sorted(w["sources"]),
        "date_added": today,
        "possible_duplicate_of": sorted(w.get("possible_duplicate_of") or []),
        "openalex_id": w["openalex_id"],
    }
    return entry


def write_report(path, works, status):
    """Markdown table for reviewers: why each new work was found and any likely duplicates."""
    cell = lambda s: (s or "").replace("|", "\\|").replace("\n", " ")
    lines = [f"## {len(works)} new publication(s), added with status `{status}`", ""]
    if works:
        lines += ["| Year | DOI | Title | Why it matched | Possible duplicate of |", "|---|---|---|---|---|"]
    for w in works:
        doi = f"[{w['doi']}](https://doi.org/{w['doi']})" if w["doi"] else ""
        dup = ", ".join(sorted(w.get("possible_duplicate_of") or []))
        lines.append(f"| {w['year'] or ''} | {doi} | {cell(w['title'])[:110]} | {cell('; '.join(w['match_reasons']))} | {dup} |")
    text = "\n".join(lines) + "\n"
    for target, mode in ((path, "w"), (os.environ.get("GITHUB_STEP_SUMMARY"), "a")):
        if target:
            with open(target, mode, encoding="utf-8") as f:
                f.write(text)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--authors", default="hakai_authors.csv")
    ap.add_argument("--publications", default="publications.json")
    ap.add_argument("--output", default=None, help="defaults to --publications")
    ap.add_argument("--full", action="store_true", help="ignore last_run and scan all years")
    ap.add_argument("--lookback-days", type=int, default=730, help="publication-date window for incremental runs")
    ap.add_argument("--status", default=None, help="status for new entries (default: 'published' on the first run, else 'needs review')")
    ap.add_argument("--report", default=None, help="write a markdown table of the new works and why they matched")
    ap.add_argument("--dry-run", action="store_true", help="print a summary and write nothing")
    args = ap.parse_args()
    output = args.output or args.publications

    by_orcid, name_only = load_authors(args.authors)
    print(f"{len(by_orcid)} ORCIDs, {len(name_only)} people without ORCID (name match only)")

    try:
        with open(args.publications, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {"publications": []}
    existing = data.get("publications", [])
    existing_dois = {norm_doi(e.get("doi")) for e in existing if e.get("doi")}
    existing_cites = [(title_key(e.get("citation")), e.get("doi")) for e in existing]

    last_run = (data.get("tool") or {}).get("last_run")
    since = None
    if last_run and not args.full:
        since = (date.fromisoformat(last_run[:10]) - timedelta(days=args.lookback_days)).isoformat()
    print(f"Scanning {'works published since ' + since if since else 'all years'}")
    # the initial backfill is added as "published"; later runs need review
    status = args.status or ("needs review" if last_run else "published")

    found, failures = [], []
    for name, fetch in (("OpenAlex", fetch_openalex), ("Crossref", fetch_crossref), ("DataCite", fetch_datacite)):
        try:
            works = fetch(sorted(by_orcid), since)
            print(f"  {name}: {len(works)} works of the wanted types")
            found += works
        except Exception as e:  # keep going with the other sources
            failures.append(name)
            warn(f"{name} failed: {e!r}; continuing with the other sources")
    if len(failures) == 3:
        sys.exit("All sources failed; nothing written")

    merged = merge(found)
    kept = [w for w in merged if annotate(w, by_orcid, name_only) and not is_unwanted_zenodo(w)]
    new = [w for w in kept if not (w["doi"] and w["doi"] in existing_dois)]
    print(f"{len(merged)} unique works, {len(kept)} with Hakai affiliation, {len(new)} not yet in {args.publications}")

    # flag (never drop) likely preprint/article pairs, within the new works and against existing entries
    new_doi_by_title = {}
    for w in new:
        new_doi_by_title.setdefault(title_key(w["title"]), []).append(w["doi"])
    for w in new:
        tk, dup = title_key(w["title"]), set()
        dup |= {d for d in new_doi_by_title.get(tk, []) if d and d != w["doi"]}
        dup |= {d for d in w["related_dois"] if d}
        if len(tk) > 20:
            dup |= {d for ek, d in existing_cites if d and tk in ek}
        w["possible_duplicate_of"] = dup

    today = date.today().isoformat()
    new = sorted(new, key=lambda w: (-(w["year"] or 0), w["title"]))
    entries = [to_entry(w, today, status) for w in new]
    print(f"New entries get status '{status}'")
    for e in entries[:5]:
        print(f"  + [{e['year']}] {e['citation'][:140]}")
    if args.dry_run:
        print("Dry run: nothing written")
        return
    write_report(args.report, new, status)

    data["publications"] = existing + entries
    data["publication_count"] = sum(1 for e in data["publications"] if e.get("status") != "rejected")
    data["last_updated"] = datetime.now().strftime("%b %d, %Y")
    # only advance last_run when every source answered, so a failed source is retried in full
    if not failures:
        data["tool"] = {**(data.get("tool") or {}), "last_run": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    with open(output, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Wrote {len(entries)} new entries to {output}")


if __name__ == "__main__":
    main()
