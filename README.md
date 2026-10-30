# hakai-publications

Finds publications with at least one Hakai-affiliated author and keeps them in
[`publications.json`](publications.json).

`gather_publications.py` searches OpenAlex, Crossref and DataCite, and appends works that are not
yet in the list. Each entry has an APA 7 citation, the DOI and structured author data. A GitHub
Action runs it monthly and opens a pull request for review.

The full requirements and design decisions are in [SPEC.md](SPEC.md).

## Files

| File | Purpose |
|---|---|
| `gather_publications.py` | The tool. Python 3, standard library only, nothing to install. |
| `publications.json` | The publication list. This is the source of truth and the tool's memory. |
| `hakai_authors.csv` | Hakai researchers and their ORCIDs, used to search for their work. |
| `.github/workflows/gather-publications.yml` | Scheduled and manual runs. |
| `SPEC.md` | Specification. |

## How a work gets in

A work is added only if **at least one author lists Hakai on that work** (ROR `02pry0c91`, or
"Hakai" in the affiliation text). Having an ORCID in `hakai_authors.csv` is not enough on its own.
A researcher who has left Hakai does not bring in papers that don't list Hakai.

Included: articles, preprints, books, book chapters, theses, reports, conference papers and
protocols. Excluded: datasets, data management plans, and Zenodo records (unless a thesis or
protocol).

Works that are already in `publications.json` (by DOI) are never added again, including
rejected ones. A preprint and its published article are both kept and flagged with
`possible_duplicate_of`.

## Reviewing entries

Each entry has a `status`:

| Status | Meaning |
|---|---|
| `published` | Accepted into the curated list. |
| `needs review` | Found by a scheduled run and not yet checked. |
| `rejected` | Wrong. Keep it in the file so the tool never adds it again. |

The first run (the initial backfill) adds entries as `published`. Every later run adds them as
`needs review`. `match_reasons` on each entry shows why it was found (ORCID, affiliation, or an
unverified name-only match).

To reject a work, change its `status` to `rejected`. Don't delete the entry, or the next run will
probably find it again.

The tool never edits or removes existing entries, so hand edits are kept.

## Running it

```
python gather_publications.py --dry-run     # summary only, writes nothing
python gather_publications.py               # updates publications.json
python gather_publications.py --full        # scan all years
```

A full run takes a few minutes. Options:

| Option | Meaning |
|---|---|
| `--dry-run` | Print a summary and write nothing. |
| `--full` | Ignore `tool.last_run` and scan all years. |
| `--lookback-days N` | Publication-date window for incremental runs (default 730). |
| `--status STATUS` | Status for new entries. Defaults to `published` on the first run, otherwise `needs review`. |
| `--authors`, `--publications`, `--output` | File paths. `--output` defaults to `--publications`. |

Optional environment variables:

- `OPENALEX_API_KEY`: a free key from <https://openalex.org/settings/api>. It raises the OpenAlex
  daily limit but is not required.
- `GATHER_MAILTO`: a contact address sent to OpenAlex and Crossref.

If a source fails, the run continues with the others and prints a warning. If any source failed,
the next run scans in full again.

## Output

Entries keep the original fields (`year`, `citation`, `url`, `doi`, `status`) and add `title`,
`authors` (name, ORCID, Hakai flag), `hakai_authors`, `venue`, `type`, `sources`,
`match_reasons`, `date_added`, and `possible_duplicate_of` when there is a likely duplicate. The
file also records `publication_count`, `last_updated` and `tool.last_run`. See SPEC.md for the
details.

## Monthly run on GitHub

The workflow runs on the 1st of each month at 14:00 UTC. To run it by hand, open the Actions tab,
choose "Gather publications" and click "Run workflow". The "full scan" checkbox rescans all years.

When there are new works, the workflow pushes the branch `auto/new-publications` and opens (or
updates) one pull request. Review the new entries in the pull request, set each `status`, then
merge. `tool.last_run` only advances when the pull request is merged, so an unmerged pull request
is simply regenerated on the next run.

Setup:

1. In Settings > Actions > General, allow GitHub Actions to create pull requests.
2. Optional: add the secret `OPENALEX_API_KEY` and the variable `GATHER_MAILTO`.
3. Scheduled workflows run only from the default branch, so merge the workflow file first.

## Keeping the author list up to date

Add new researchers to `hakai_authors.csv` with their ORCID. People without an ORCID can be
listed by name, but they only add an unverified note to `match_reasons` and never cause a work
to be added.

## Limitations

- Affiliation matching depends on how each source records affiliations. A work whose record
  omits Hakai is missed. Against the first 71 curated entries, the tool found 67.
- Name formatting for OpenAlex authors is approximate for unusual surnames.
- Titles are used as the source gives them, so capitalisation may not follow APA sentence case.
