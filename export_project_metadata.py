"""Export Hakai Catalogue metadata records for the "Geospatial" and "ACO"
(Airborne Coastal Observatory) projects to an .xlsx file.

Downloads every .xml file from the "hakai" folder of the public repo
HakaiInstitute/hakai-metadata-entry-form-files, and keeps records whose
project keyword(s) include "Geospatial" and/or "Airborne Coastal Observatory"
(a record tagged with either project is included - this is a union, not an
intersection). For each matching record, extracts the title, the full list
of project tags on that record, and its DOI.

Usage:
    python export_project_metadata.py [output.xlsx]
"""

import sys
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

from openpyxl import Workbook

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

TARGET_PROJECTS = {"geospatial", "airborne coastal observatory", "aco"}


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "hakai-publications-script"})
    with urllib.request.urlopen(req, timeout=30) as r:
        import json

        return json.load(r)


def list_xml_paths():
    tree = fetch_json(TREE_URL)["tree"]
    return [t["path"] for t in tree if t["path"].startswith(f"{DIR}/") and t["path"].endswith(".xml")]


def fetch_text(path):
    url = RAW_URL.format(path=path)
    req = urllib.request.Request(url, headers={"User-Agent": "hakai-publications-script"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="ignore")


def extract_record(content):
    """Return (title, doi, projects) for a metadata record, or None if unparsable."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return None

    citation = root.find(CITATION_PATH, NS)
    title = None
    doi = None
    if citation is not None:
        title_el = citation.find("./cit:title/gco:CharacterString", NS)
        title = title_el.text if title_el is not None else None
        for code_el in citation.findall("./cit:identifier/mcc:MD_Identifier/mcc:code/gco:CharacterString", NS):
            if code_el.text and "doi.org" in code_el.text.lower():
                doi = code_el.text.strip()
                break

    projects = []
    for dk in root.findall(".//mri:descriptiveKeywords/mri:MD_Keywords", NS):
        type_el = dk.find("./mri:type/mri:MD_KeywordTypeCode", NS)
        if type_el is None or type_el.get("codeListValue") != "project":
            continue
        for kw in dk.findall("./mri:keyword/gco:CharacterString", NS):
            if kw.text:
                projects.append(kw.text.strip())

    return {"title": title, "doi": doi, "projects": projects}


def matches_target_projects(projects):
    return any(p.lower() in TARGET_PROJECTS for p in projects)


def write_xlsx(rows, output_path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Geospatial & ACO Records"
    ws.append(["Title", "Project", "DOI"])
    for row in rows:
        ws.append([row["title"], row["project"], row["doi"]])
    for column_cells in ws.columns:
        max_len = max(len(str(cell.value)) if cell.value else 0 for cell in column_cells)
        ws.column_dimensions[column_cells[0].column_letter].width = min(max_len + 2, 80)
    wb.save(output_path)


def main():
    output_path = sys.argv[1] if len(sys.argv) > 1 else "geospatial_aco_records.xlsx"

    print(f"Listing XML files in {REPO}/{DIR} ...")
    paths = list_xml_paths()
    print(f"Found {len(paths)} XML files.")

    with ThreadPoolExecutor(max_workers=16) as ex:
        contents = list(ex.map(fetch_text, paths))

    rows = []
    for content in contents:
        record = extract_record(content)
        if record is None:
            continue
        if matches_target_projects(record["projects"]):
            rows.append(
                {
                    "title": record["title"],
                    "project": "; ".join(record["projects"]),
                    "doi": record["doi"],
                }
            )

    rows.sort(key=lambda r: (r["title"] or "").lower())
    write_xlsx(rows, output_path)

    print(f"{len(rows)} of {len(paths)} records matched the Geospatial/ACO projects.")
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
