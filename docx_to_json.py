"""Convert the "Nearshore Publications.docx" bibliography into structured JSON.

The source document organizes citations under Heading 1 section titles
(e.g. "Seagrass and Soft-Sediments"), with plain-text year markers (e.g.
"2025", "2018 & earlier") introducing groups of citations. Individual
citations are usually separated by blank paragraphs, but some are wrapped
across multiple paragraphs/line-breaks with no blank separator, and a few
paragraphs pack more than one citation together. The parsing logic below
handles all of these cases heuristically.

Usage:
    python docx_to_json.py [input.docx] [output.json]

Defaults to "Nearshore Publications.docx" -> "publications.json".
"""

import json
import re
import sys

from docx import Document

YEAR_LINE_RE = re.compile(r"^(\d{4})(\s*&\s*earlier)?$")
YEAR_ANYWHERE_RE = re.compile(r"\b(19|20)\d{2}\b")
URL_RE = re.compile(r"https?://\S+")
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s,;]+")
NEW_CITATION_START_RE = re.compile(
    r"^(In Review\.?|In Press\.?|Submitted\b|"
    r"[A-Z][A-Za-z\-'’]+,?\s+[A-Z]{1,3}\.)"
)
ZERO_WIDTH_SPACE = "​"


def clean(text):
    return text.replace(ZERO_WIDTH_SPACE, "").strip()


def looks_complete(text):
    if URL_RE.search(text):
        return True
    stripped = text.rstrip()
    if stripped.lower().endswith("pdf") and YEAR_ANYWHERE_RE.search(text):
        return True
    if stripped.endswith(".") and YEAR_ANYWHERE_RE.search(text):
        return True
    return False


def strip_trailing_punct(value):
    return value.rstrip(".,;) ") if value else value


def extract_metadata(citation):
    url_match = URL_RE.search(citation)
    doi_match = DOI_RE.search(citation)
    url = strip_trailing_punct(url_match.group(0)) if url_match else None
    doi = strip_trailing_punct(doi_match.group(0)) if doi_match else None

    lowered = citation.lower()
    if "in review" in lowered:
        status = "in review"
    elif "in press" in lowered:
        status = "in press"
    elif "submitted" in lowered:
        status = "submitted"
    elif "preprint" in lowered:
        status = "preprint"
    else:
        status = "published"

    return url, doi, status


def flatten_lines(doc):
    """Yield (style_name, text) pairs, splitting embedded line breaks."""
    for paragraph in doc.paragraphs:
        style = paragraph.style.name
        raw = paragraph.text
        for index, sub in enumerate(raw.split("\n")):
            yield (style if index == 0 else "normal", clean(sub))


def parse(doc):
    entries = []
    section = None
    year = None
    buffer = []

    def flush():
        if not buffer:
            return
        text = re.sub(r"\s+", " ", " ".join(buffer)).strip()
        buffer.clear()
        if not text:
            return
        entry_year = year
        if entry_year is None:
            match = YEAR_ANYWHERE_RE.search(text)
            entry_year = match.group(0) if match else None
        url, doi, status = extract_metadata(text)
        entries.append(
            {
                "section": section,
                "year": entry_year,
                "citation": text,
                "url": url,
                "doi": doi,
                "status": status,
            }
        )

    for style, text in flatten_lines(doc):
        if style == "Heading 1":
            flush()
            section = text
            year = None
            continue
        if section is None:
            # Skip title/subtitle/table-of-contents content before the
            # first section heading.
            continue
        if text == "":
            flush()
            continue
        if YEAR_LINE_RE.match(text):
            flush()
            year = text
            continue
        if buffer and looks_complete(" ".join(buffer)) and NEW_CITATION_START_RE.match(text):
            flush()
        buffer.append(text)

    flush()
    return entries


def extract_last_updated(doc):
    for paragraph in doc.paragraphs[:5]:
        match = re.search(r"Last updated:\s*(.+)", paragraph.text)
        if match:
            return match.group(1).strip()
    return None


def main():
    input_path = sys.argv[1] if len(sys.argv) > 1 else "Nearshore Publications.docx"
    output_path = sys.argv[2] if len(sys.argv) > 2 else "publications.json"

    doc = Document(input_path)
    publications = [
        p for p in parse(doc) if p["status"] == "published" and p["doi"] is not None
    ]
    output = {
        "last_updated": extract_last_updated(doc),
        "publication_count": len(publications),
        "publications": publications,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"Wrote {len(publications)} publications to {output_path}")


if __name__ == "__main__":
    main()
