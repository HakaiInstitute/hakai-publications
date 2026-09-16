"""Check which publication DOIs (from publications.json) appear in the Hakai
metadata-entry-form XML records.

Downloads every .xml file from the "hakai" folder of the public repo
HakaiInstitute/hakai-metadata-entry-form-files, and checks each publication's
DOI (from publications.json) for a literal match in the XML content. A match
means that dataset's metadata record cites the publication (typically as a
"largerWorkCitation" associated resource).

Usage:
    python scrape_metadata_dois.py [publications.json] [output.json]
"""

import json
import sys
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

REPO = "HakaiInstitute/hakai-metadata-entry-form-files"
BRANCH = "main"
DIR = "hakai"
TREE_URL = f"https://api.github.com/repos/{REPO}/git/trees/{BRANCH}?recursive=1"
RAW_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{{path}}"

NS = {
    "mdb": "http://standards.iso.org/iso/19115/-3/mdb/2.0",
    "mri": "http://standards.iso.org/iso/19115/-3/mri/1.0",
    "cit": "http://standards.iso.org/iso/19115/-3/cit/2.0",
    "mcc": "http://standards.iso.org/iso/19115/-3/mcc/1.0",
    "gco": "http://standards.iso.org/iso/19115/-3/gco/1.0",
}
CITATION_PATH = "./mdb:identificationInfo/mri:MD_DataIdentification/mri:citation/cit:CI_Citation"


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "hakai-publications-script"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def list_xml_paths():
    tree = fetch_json(TREE_URL)["tree"]
    return [t["path"] for t in tree if t["path"].startswith(f"{DIR}/") and t["path"].endswith(".xml")]


def fetch_text(path):
    url = RAW_URL.format(path=path)
    req = urllib.request.Request(url, headers={"User-Agent": "hakai-publications-script"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="ignore")


def extract_record_info(content):
    """Return (title, doi) for the metadata record itself, or (None, None)."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return None, None

    citation = root.find(CITATION_PATH, NS)
    if citation is None:
        return None, None

    title_el = citation.find("./cit:title/gco:CharacterString", NS)
    title = title_el.text if title_el is not None else None

    doi = None
    for code_el in citation.findall("./cit:identifier/mcc:MD_Identifier/mcc:code/gco:CharacterString", NS):
        if code_el.text and "doi.org" in code_el.text.lower():
            doi = code_el.text.strip()
            break

    return title, doi


def main():
    pubs_path = sys.argv[1] if len(sys.argv) > 1 else "publications.json"
    output_path = sys.argv[2] if len(sys.argv) > 2 else "publication_metadata_matches.json"

    publications = json.load(open(pubs_path, encoding="utf-8"))["publications"]
    dois = [p["doi"] for p in publications if p.get("doi")]

    print(f"Checking {len(dois)} DOIs against XML files in {REPO}/{DIR} ...")
    paths = list_xml_paths()
    print(f"Found {len(paths)} XML files.")

    with ThreadPoolExecutor(max_workers=16) as ex:
        contents = list(ex.map(fetch_text, paths))

    files_by_path = dict(zip(paths, contents))
    record_info_by_path = {path: extract_record_info(content) for path, content in files_by_path.items()}

    results = []
    for pub in publications:
        doi = pub.get("doi")
        if not doi:
            continue
        matched_files = [
            {
                "path": path,
                "record_title": record_info_by_path[path][0],
                "record_doi": record_info_by_path[path][1],
            }
            for path, content in files_by_path.items()
            if doi.lower() in content.lower()
        ]
        if matched_files:
            results.append(
                {
                    "doi": doi,
                    "citation": pub["citation"],
                    "matched_files": matched_files,
                }
            )

    output = {
        "xml_file_count": len(paths),
        "dois_checked": len(dois),
        "dois_matched": len(results),
        "matches": results,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"{len(results)} of {len(dois)} publications matched at least one XML file.")
    print(f"Wrote results to {output_path}")


if __name__ == "__main__":
    main()
