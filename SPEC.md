# Publication gatherer: specification

Periodically finds new publications with at least one Hakai-affiliated author and adds them to
`publications.json` for review.

Implementation: `gather_publications.py` (Python, standard library only), run by
`.github/workflows/gather-publications.yml`.

## 1. Inputs

| Input | Purpose |
|---|---|
| `hakai_authors.csv` | Hakai researchers (`author`, `orcid`, `Affilation`). ORCIDs may be bare or full URLs. Rows sharing an ORCID are merged. Rows without an ORCID are matched by name only. |
| `publications.json` | The curated list, and the tool's memory (see section 6). |
| `OPENALEX_API_KEY` (optional secret) | Raises the OpenAlex daily budget. Not required. |
| `GATHER_MAILTO` (optional variable) | Contact address for the OpenAlex and Crossref polite pools. |

## 2. Sources and search

OpenAlex, Crossref and DataCite are all queried. Each source is searched two ways:

1. **By ORCID** for every ORCID in `hakai_authors.csv`.
2. **By Hakai affiliation**: the ROR ID `02pry0c91`, or "Hakai" in the affiliation text.

Notes on behaviour:

- OpenAlex uses page-number paging (cursor paging needs an API key). This is fine below 10,000 results per query.
- Crossref has no affiliation filter. The tool pages through a relevance-ranked affiliation search and stops once a page contains no Hakai-affiliated author.
- DataCite searches creators only.
- People without an ORCID trigger no search. Their names are compared against works already found, and a match is recorded in `match_reasons` as "name only (unverified)".

## 3. Inclusion rule

A work is added only if **at least one author lists Hakai on that work** (ROR `02pry0c91`, or "Hakai" in the affiliation text).

- A listed ORCID alone never qualifies a work. A researcher who has left Hakai does not bring in papers that do not list Hakai.
- The `hakai` flag on an author, and `hakai_authors`, follow the affiliation on that work only, not membership in `hakai_authors.csv`.
- ORCID matches are still recorded in `match_reasons` ("ORCID: Name") to show how a work was found.

## 4. Work types

**Included:** articles (including reviews, letters, editorials), preprints, books, book chapters, theses, reports, conference papers, and protocols (protocols.io DOIs, prefix `10.17504/`).

**Excluded:**
- datasets, peer reviews, errata, grants, supplementary materials
- data management plans (DMPTool DOIs, prefix `10.48321/`, or a resource type containing "plan")
- Zenodo records (`10.5281/`) unless they are a thesis or protocol
- DataCite version DOIs (`IsVersionOf`)

## 5. Deduplication

- Works are merged across sources by normalised DOI (lowercase, no URL prefix). Works without a DOI are merged by title and year.
- A work whose DOI is already in `publications.json` is never added again. This includes entries with status `rejected`.
- A preprint and its published article are **both kept**. Each new entry is flagged with `possible_duplicate_of` (the other DOI) when:
  - the titles match (against other new works and against existing entries), or
  - Crossref or DataCite declares a preprint relationship.

## 6. Output: `publications.json`

The existing fields are kept so that other scripts keep working: `year`, `citation`, `url`, `doi`, `status`. `section` is **not** set on new entries (see section 10).

Added fields:

| Field | Content |
|---|---|
| `title`, `venue`, `type` | Bibliographic data. `type` is one of: article, preprint, book, book-chapter, thesis, report, conference-paper, protocol. |
| `authors` | List of `{name, orcid?, hakai}`. |
| `hakai_authors` | Names of the authors with a Hakai affiliation on that work. |
| `sources` | Which of OpenAlex, Crossref and DataCite returned the work. |
| `match_reasons` | Why the work was found (ORCID, affiliation, name only). |
| `date_added` | ISO date. |
| `possible_duplicate_of` | Only present when a likely duplicate exists. |
| `openalex_id` | When available. |

File-level fields: `publication_count` (entries not `rejected`), `last_updated`, and `tool.last_run`.

Existing entries are never edited or removed. Hand edits are preserved. New entries are appended, sorted by year (newest first).

**Citation** is plain-text APA 7 with no markup: authors, year, title, venue, volume(issue), pages, DOI URL. Initials follow the given name. With more than 20 authors, the first 19, ". . .", then the last author. Source priority for fields is OpenAlex, then Crossref, then DataCite.

## 7. Status and review

| Status | Meaning |
|---|---|
| `published` | Accepted into the curated list. |
| `needs review` | Found by the tool on a later run, not yet checked. |
| `rejected` | Reviewed and wrong. Kept in the file so the tool never re-adds it. |

- **The initial backfill** (a run with no `tool.last_run` in the file) adds entries as `published`.
- **Every later run** adds entries as `needs review`.
- `--status` overrides this.

## 8. Runs

- **Schedule:** monthly, on the 1st at 14:00 UTC.
- **Manual:** the "Run workflow" button in the Actions tab, with a "full scan" option.
- **First run:** all years.
- **Later runs:** works published within the last 730 days (`--lookback-days`), so late-indexed works are still caught. Duplicates are harmless because of section 5. `--full` forces an all-years scan.
- **Delivery:** the Action pushes to the fixed branch `auto/new-publications` and opens or updates one pull request. Nothing reaches the default branch unreviewed. `tool.last_run` only advances when the pull request is merged, so an unmerged pull request is regenerated with anything newer on the next run.

## 9. Failure handling

- A failing source is reported as a warning (a GitHub annotation in the Action), and the run continues with the other sources.
- If any source failed, `tool.last_run` is not advanced, so the next run re-scans in full.
- If all sources fail, nothing is written and the run fails.
- HTTP calls retry with backoff on 429 and 5xx responses, and errors include the server's message.

## 10. Out of scope (possible later work)

- **Section assignment.** `section` is a manual, editorial field from the original Word document, and the tool does not set it. Suggesting it automatically (from topics, keywords or a model reading the abstract) was deferred.
- **Hakai Catalogue linking.** Marking whether a work is in the catalogue is done in `search-pubs.R` and `scrape_metadata_dois.py`, not in this tool.
- **Journal and book-title italics.** The citation is plain text, so none are stored.

## 11. Known limitations

- Affiliation matching depends on how each source records affiliations. A work whose record omits Hakai is missed. Against the 71 curated entries, the tool found 67.
- Name-only matching is unverified and only annotates `match_reasons`. It never adds a work.
- OpenAlex author names are split as given name and family name by the last word, with common particles (van, de, der, ...) kept in the family name. Unusual names may be formatted imperfectly.
- Titles are used as the source gives them, so capitalisation may not follow APA sentence case.
