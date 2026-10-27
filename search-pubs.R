# hakai_genomics_pubs.R
# Pull works for the Hakai genomics team from OpenAlex, Crossref and DataCite,
# flag Hakai affiliation (ROR 02pry0c91 or "Hakai" in affiliation text),
# and deduplicate across sources.

library(httr2)
library(dplyr)
library(purrr)
library(stringr)
library(readr)

mailto <- "tim.vanderstap@hakai.org" # polite-pool contact for OpenAlex / Crossref

team <- tibble::tribble(
  ~name                    , ~orcid                ,
  "Matthew Lemay"          , "0000-0001-7051-0020" ,
  "Colleen Kellogg"        , "0000-0003-4048-5316" ,
  "Tyler Murchie"          , "0000-0003-1104-4597" ,
  "Rute Clemente-Carvalho" , "0000-0001-6922-9418" ,
  "Evan Morien"            , "0000-0001-9821-7002" ,
  "Libby Natola"           , "0000-0001-7576-2825"
)
hakai_ror <- "02pry0c91"

# ---- helpers -----------------------------------------------------------------
norm_doi <- function(x) {
  str_remove(str_to_lower(x), "^https?://(dx\\.)?doi\\.org/")
}
norm_orcid <- function(x) str_extract(x, "\\d{4}-\\d{4}-\\d{4}-\\d{3}[\\dX]")

team_hits <- function(orcids) {
  hits <- intersect(norm_orcid(orcids), team$orcid)
  paste(team$name[team$orcid %in% hits], collapse = "; ")
}

is_hakai <- function(x) {
  x <- unlist(x)
  any(str_detect(x, hakai_ror), na.rm = TRUE) ||
    any(str_detect(x, regex("hakai", ignore_case = TRUE)), na.rm = TRUE)
}

get_json <- function(url, query = list()) {
  request(url) |>
    req_url_query(!!!query) |>
    req_user_agent(paste0("hakai-genomics-pubs (mailto:", mailto, ")")) |>
    req_throttle(rate = 5) |>
    req_retry(max_tries = 4) |>
    req_perform() |>
    resp_body_json()
}

# ---- OpenAlex ----------------------------------------------------------------
openalex_works <- function(orcid) {
  out <- list()
  cursor <- "*"
  while (!is.null(cursor)) {
    j <- get_json(
      "https://api.openalex.org/works",
      list(
        filter = paste0("author.orcid:", orcid),
        `per-page` = 200,
        cursor = cursor,
        mailto = mailto
      )
    )
    out <- c(out, j$results)
    cursor <- j$meta$next_cursor
  }
  map_dfr(out, \(w) {
    au <- w$authorships %||% list()
    tibble(
      doi = norm_doi(pluck(w, "doi", .default = NA_character_)),
      openalex_id = pluck(w, "id", .default = NA_character_),
      title = pluck(w, "display_name", .default = NA_character_),
      year = as.integer(pluck(w, "publication_year", .default = NA)),
      type = pluck(w, "type", .default = NA_character_),
      venue = pluck(
        w,
        "primary_location",
        "source",
        "display_name",
        .default = NA_character_
      ),
      team_authors = team_hits(map_chr(au, \(a) {
        pluck(a, "author", "orcid", .default = NA_character_)
      })),
      hakai = is_hakai(c(
        map(au, \(a) map(a$institutions, "ror")),
        map(au, "raw_affiliation_strings")
      )),
      source = "OpenAlex"
    )
  })
}

# ---- Crossref ----------------------------------------------------------------
# Only finds works where the publisher deposited the ORCID.
crossref_works <- function(orcid) {
  out <- list()
  cursor <- "*"
  repeat {
    j <- get_json(
      "https://api.crossref.org/works",
      list(
        filter = paste0("orcid:", orcid),
        rows = 1000,
        cursor = cursor,
        mailto = mailto
      )
    )
    items <- j$message$items
    out <- c(out, items)
    cursor <- j$message$`next-cursor`
    if (length(items) < 1000 || is.null(cursor)) break
  }
  map_dfr(out, \(w) {
    au <- w$author %||% list()
    tibble(
      doi = norm_doi(w$DOI),
      title = pluck(w, "title", 1, .default = NA_character_),
      year = as.integer(pluck(w, "issued", "date-parts", 1, 1, .default = NA)),
      type = pluck(w, "type", .default = NA_character_),
      venue = pluck(
        w,
        "container-title",
        1,
        .default = pluck(w, "publisher", .default = NA_character_)
      ),
      team_authors = team_hits(map_chr(au, \(a) {
        pluck(a, "ORCID", .default = NA_character_)
      })),
      hakai = is_hakai(map(au, "affiliation")),
      source = "Crossref"
    )
  })
}

# ---- DataCite ----------------------------------------------------------------
# Datasets, Zenodo deposits, preprints etc. Searches creators and contributors.
datacite_works <- function(orcid) {
  id <- paste0('"https://orcid.org/', orcid, '"')
  url <- "https://api.datacite.org/dois"
  q <- list(
    query = paste0(
      "creators.nameIdentifiers.nameIdentifier:",
      id,
      " OR contributors.nameIdentifiers.nameIdentifier:",
      id
    ),
    `page[size]` = 1000,
    `page[cursor]` = 1,
    affiliation = "true"
  )
  out <- list()
  repeat {
    j <- get_json(url, q)
    out <- c(out, j$data)
    nxt <- j$links$`next`
    if (is.null(nxt) || length(j$data) == 0) {
      break
    }
    url <- nxt
    q <- list()
  }
  map_dfr(out, \(d) {
    a <- d$attributes
    people <- c(a$creators %||% list(), a$contributors %||% list())
    pub <- pluck(a, "publisher", .default = NA_character_)
    if (is.list(pub)) {
      pub <- pluck(pub, "name", .default = NA_character_)
    }
    tibble(
      doi = norm_doi(a$doi),
      title = pluck(a, "titles", 1, "title", .default = NA_character_),
      year = as.integer(pluck(a, "publicationYear", .default = NA)),
      type = pluck(a, "types", "resourceTypeGeneral", .default = NA_character_),
      venue = pub,
      team_authors = team_hits(unlist(map(people, \(p) {
        map(p$nameIdentifiers, "nameIdentifier")
      }))),
      hakai = is_hakai(map(people, "affiliation")),
      # Zenodo-style version DOIs point to a concept DOI; drop them later
      is_version = "IsVersionOf" %in%
        map_chr(a$relatedIdentifiers %||% list(), \(r) {
          r$relationType %||% NA_character_
        }),
      source = "DataCite"
    )
  })
}

# ---- run ---------------------------------------------------------------------
raw <- bind_rows(
  map(team$orcid, openalex_works),
  map(team$orcid, crossref_works),
  map(team$orcid, datacite_works)
)
count(raw, source)

title_key <- function(t) str_to_lower(str_replace_all(t, "[^[:alnum:]]", ""))

pubs <- raw |>
  filter(!coalesce(is_version, FALSE)) |>
  mutate(key = coalesce(doi, paste0("title:", title_key(title), "-", year))) |>
  group_by(key) |>
  summarise(
    doi = first(doi),
    title = first(na.omit(title)),
    year = first(na.omit(year)),
    type = paste(unique(na.omit(type)), collapse = " / "),
    venue = first(na.omit(venue)),
    team_authors = team_authors |>
      str_split("; ") |>
      unlist() |>
      setdiff("") |>
      unique() |>
      sort() |>
      paste(collapse = "; "),
    hakai_affiliated = any(hakai, na.rm = TRUE),
    sources = paste(sort(unique(source)), collapse = "; "),
    openalex_id = first(na.omit(openalex_id)),
    .groups = "drop"
  ) |>
  select(-key) |>
  # same title + year under different DOIs (e.g. preprint vs. article): flag, don't drop
  group_by(tk = title_key(title)) |>
  mutate(possible_duplicate = n() > 1) |>
  ungroup() |>
  select(-tk) |>
  arrange(desc(year), title)

pubs |>
  mutate(doi = if_else(is.na(doi), doi, paste0("https://doi.org/", doi))) |>
  write_csv("hakai_genomics_pubs_all.csv")

pubs |> count(hakai_affiliated, sources)
