# hakai_genomics_pubs.R
# Pull works for the Hakai genomics team from OpenAlex, Crossref and DataCite,
# flag Hakai affiliation (ROR 02pry0c91 or "Hakai" in affiliation text),
# and deduplicate across sources.

library(httr2)
library(dplyr)
library(purrr)
library(stringr)
library(readr)
library(openxlsx2)

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

nz <- function(x) !is.null(x) && length(x) == 1 && !is.na(x) && nzchar(x)

# "Family, G.-H." with markdown bold when the author is on the team or has a
# Hakai affiliation
fmt_author <- function(family, given, bold) {
  family <- if (nz(family)) str_squish(family) else "Unknown"
  ini <- ""
  if (nz(given)) {
    tok <- str_split_1(str_squish(given), "[\\s-]+")
    ini <- paste0(substr(tok[nzchar(tok)], 1, 1), ".", collapse = " ")
  }
  nm <- if (nzchar(ini)) paste0(family, ", ", ini) else family
  if (bold) paste0("**", nm, "**") else nm
}

is_bold_author <- function(orcid, affiliation) {
  isTRUE(norm_orcid(orcid) %in% team$orcid) || is_hakai(affiliation)
}

build_citation <- function(authors, year, title, venue, volume, issue, pages, doi) {
  # long author lists: keep only the bolded (Hakai) authors, then "et al."
  et_al <- length(authors) > 10
  if (et_al) {
    authors <- authors[str_starts(authors, fixed("**"))]
  }
  n <- length(authors)
  a <- if (et_al) {
    paste0(paste(authors, collapse = ", "), ", et al.")
  } else if (n <= 1) {
    paste(authors, collapse = "")
  } else {
    paste(paste(authors[-n], collapse = ", "), "&", authors[n])
  }
  cit <- paste0(a, " (", year, "). ", title, ".")
  if (nz(venue)) {
    cit <- paste0(cit, " ", venue)
    if (nz(volume)) cit <- paste0(cit, ", ", volume)
    if (nz(issue)) cit <- paste0(cit, "(", issue, ")")
    if (nz(pages)) cit <- paste0(cit, ", ", pages)
    cit <- paste0(cit, ".")
  }
  if (nz(doi)) cit <- paste0(cit, " https://doi.org/", doi)
  str_squish(cit)
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
    doi <- norm_doi(pluck(w, "doi", .default = NA_character_))
    title <- pluck(w, "display_name", .default = NA_character_)
    year <- as.integer(pluck(w, "publication_year", .default = NA))
    venue <- pluck(
      w,
      "primary_location",
      "source",
      "display_name",
      .default = NA_character_
    )
    authors <- map_chr(au, \(a) {
      # OpenAlex only gives a display name: last word is taken as the family name
      parts <- str_split_1(
        str_squish(pluck(a, "author", "display_name", .default = "")),
        " "
      )
      fmt_author(
        family = parts[length(parts)],
        given = if (length(parts) > 1) paste(parts[-length(parts)], collapse = " "),
        bold = is_bold_author(
          pluck(a, "author", "orcid", .default = NA_character_),
          c(map(a$institutions, "ror"), a$raw_affiliation_strings)
        )
      )
    })
    first_page <- pluck(w, "biblio", "first_page", .default = NA_character_)
    last_page <- pluck(w, "biblio", "last_page", .default = NA_character_)
    tibble(
      doi = doi,
      openalex_id = pluck(w, "id", .default = NA_character_),
      title = title,
      year = year,
      type = pluck(w, "type", .default = NA_character_),
      venue = venue,
      citation = build_citation(
        authors,
        year,
        title,
        venue,
        pluck(w, "biblio", "volume", .default = NA_character_),
        pluck(w, "biblio", "issue", .default = NA_character_),
        if (nz(first_page)) {
          if (nz(last_page) && last_page != first_page) {
            paste0(first_page, "-", last_page)
          } else {
            first_page
          }
        },
        doi
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
    doi <- norm_doi(w$DOI)
    title <- pluck(w, "title", 1, .default = NA_character_)
    year <- as.integer(pluck(w, "issued", "date-parts", 1, 1, .default = NA))
    venue <- pluck(
      w,
      "container-title",
      1,
      .default = pluck(w, "publisher", .default = NA_character_)
    )
    authors <- map_chr(au, \(a) {
      fmt_author(
        family = a$family %||% a$name,
        given = a$given,
        bold = is_bold_author(
          pluck(a, "ORCID", .default = NA_character_),
          a$affiliation
        )
      )
    })
    tibble(
      doi = doi,
      title = title,
      year = year,
      type = pluck(w, "type", .default = NA_character_),
      venue = venue,
      citation = build_citation(
        authors,
        year,
        title,
        venue,
        pluck(w, "volume", .default = NA_character_),
        pluck(w, "issue", .default = NA_character_),
        pluck(w, "page", .default = NA_character_),
        doi
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
    doi <- norm_doi(a$doi)
    title <- pluck(a, "titles", 1, "title", .default = NA_character_)
    year <- as.integer(pluck(a, "publicationYear", .default = NA))
    authors <- map_chr(a$creators %||% list(), \(p) {
      fam <- p$familyName
      giv <- p$givenName
      if (!nz(fam) && nz(p$name) && str_detect(p$name, ",")) {
        nm <- str_split_1(p$name, ",")
        fam <- nm[1]
        giv <- paste(nm[-1], collapse = " ")
      }
      fmt_author(
        family = if (nz(fam)) fam else p$name,
        given = giv,
        bold = is_bold_author(
          unlist(map(p$nameIdentifiers, "nameIdentifier")),
          p$affiliation
        )
      )
    })
    tibble(
      doi = doi,
      title = title,
      year = year,
      type = pluck(a, "types", "resourceTypeGeneral", .default = NA_character_),
      venue = pub,
      citation = build_citation(authors, year, title, pub, NA, NA, NA, doi),
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
    # sources are bound OpenAlex > Crossref > DataCite, so this prefers OpenAlex
    citation = first(na.omit(citation)),
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

# ---- Hakai Catalogue ---------------------------------------------------------
# Published metadata records live in the "hakai" folder of this public repo.
# A work counts as linked if its DOI appears anywhere in a record's XML
# (resource / related work citations, or the record's own DOI).
catalogue_repo <- "HakaiInstitute/hakai-metadata-entry-form-files"

catalogue_records <- function() {
  tree <- get_json(paste0(
    "https://api.github.com/repos/",
    catalogue_repo,
    "/git/trees/main"
  ), list(recursive = 1))$tree
  paths <- keep(map_chr(tree, "path"), \(p) {
    str_starts(p, "hakai/") && str_ends(p, "\\.xml")
  })
  ns <- c(
    mdb = "http://standards.iso.org/iso/19115/-3/mdb/2.0",
    mri = "http://standards.iso.org/iso/19115/-3/mri/1.0",
    cit = "http://standards.iso.org/iso/19115/-3/cit/2.0",
    mcc = "http://standards.iso.org/iso/19115/-3/mcc/1.0",
    gco = "http://standards.iso.org/iso/19115/-3/gco/1.0"
  )
  id_path <- paste0(
    "./mdb:identificationInfo/mri:MD_DataIdentification/mri:citation/",
    "cit:CI_Citation/cit:identifier/mcc:MD_Identifier/mcc:code/gco:CharacterString"
  )
  map_dfr(paths, \(p) {
    txt <- request(paste0(
      "https://raw.githubusercontent.com/",
      catalogue_repo,
      "/main/",
      p
    )) |>
      req_user_agent(paste0("hakai-genomics-pubs (mailto:", mailto, ")")) |>
      req_throttle(rate = 5) |>
      req_retry(max_tries = 4) |>
      req_perform() |>
      resp_body_string()
    ids <- tryCatch(
      xml2::xml_text(xml2::xml_find_all(xml2::read_xml(txt), id_path, ns)),
      error = \(e) character()
    )
    tibble(
      path = p,
      text = str_to_lower(txt),
      record_doi = first(norm_doi(ids[str_detect(ids, regex("doi\\.org", TRUE))]))
    )
  })
}

records <- catalogue_records()

# DOI of the catalogue record(s) whose XML mentions `doi`
catalogue_match <- function(doi) {
  if (is.na(doi)) {
    return(list(found = FALSE, others = NA_character_))
  }
  hit <- str_detect(records$text, paste0("\\Q", doi, "\\E(?![a-z0-9])"))
  # the work's own catalogue record is not listed, only other records citing it;
  # records without a DOI of their own are reported by file path
  other <- hit & !(records$record_doi %in% doi)
  ids <- unique(ifelse(
    is.na(records$record_doi[other]),
    records$path[other],
    paste0("https://doi.org/", records$record_doi[other])
  ))
  list(
    found = any(hit),
    others = if (length(ids) == 0) NA_character_ else paste(ids, collapse = "; ")
  )
}

out <- pubs |>
  filter(hakai_affiliated) |>
  mutate(
    matches = map(doi, catalogue_match),
    in_hakai_catalogue = map_lgl(matches, "found"),
    catalogue_record_doi = map_chr(matches, "others"),
    matches = NULL,
    doi = if_else(is.na(doi), doi, paste0("https://doi.org/", doi))
  )

# XLSX turns the **bold** markers into real bold runs
rich_text <- function(x) {
  parts <- str_split_1(x, fixed("**"))
  bold <- seq_along(parts) %% 2 == 0 # every second piece sits between a pair of **
  keep <- nzchar(parts)
  reduce(map2(parts[keep], bold[keep], \(p, b) fmt_txt(p, bold = b)), `+`)
}

cit_col <- match("citation", names(out))
wb <- wb_workbook() |>
  wb_add_worksheet("publications") |>
  wb_add_data(x = out, na.strings = "") |>
  wb_add_font(dims = wb_dims(rows = 1, cols = seq_along(out)), bold = TRUE)
for (i in seq_len(nrow(out))) {
  wb <- wb_add_data(
    wb,
    x = rich_text(out$citation[i]),
    dims = wb_dims(rows = i + 1, cols = cit_col),
    col_names = FALSE
  )
}
wb <- wb |>
  wb_set_col_widths(
    cols = seq_along(out),
    widths = c(doi = 40, title = 60, citation = 90, venue = 30, team_authors = 30)[names(out)] |>
      coalesce(16)
  ) |>
  wb_add_cell_style(
    dims = wb_dims(rows = seq_len(nrow(out)) + 1, cols = seq_along(out)),
    wrap_text = TRUE,
    vertical = "top"
  ) |>
  wb_freeze_pane(first_row = TRUE) |>
  wb_add_filter(rows = 1, cols = seq_along(out))
wb_save(wb, "hakai_genomics_pubs.xlsx", overwrite = TRUE)

pubs |> count(hakai_affiliated, sources)
