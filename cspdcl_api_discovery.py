"""CSPDCL-specific document discovery via its backend REST APIs.

CSPDCL (web.cspdcl.co.in, a React SPA) renders every document-download
button as `<a href="javascript:void(0)">` or a plain `<button>`, with the
real file fetch handled entirely through React's internal click handling --
invisible to any HTML/link-based crawling (confirmed live on its Annual
Reports page, then on 7 more pages; see notes/ISSUES_LOG.md). This module
calls each page's real backend API directly instead of simulating a click.

All endpoints were found by rendering each page with Playwright, clicking
through its UI (expanding accordions, etc.) to force the real network call,
and -- where that still showed nothing -- by downloading the site's own
main.<hash>.js bundle and grepping it for the axios base URLs
(https://escapi.cspdcl.co.in/api/api, /urja, /wrap) and the route strings
built on top of them. All are public and unauthenticated -- confirmed
directly with plain `requests`, no browser or session needed.

SECTIONS is a dict so adding another investigated page is new data, not new
code -- except for a handful of pages (tenders, recruitment, SAIFI/SAIDI
reports) whose data is nested or paginated rather than one flat list; those
get their own small discover function below, selected by a section's
"kind".

Known gap: RTI disclosures (`/urja/rti/list/4?rti_type=N`) return HTTP 500
for every rti_type tried (1, 2) -- the endpoint itself appears to be broken
server-side, not a discovery problem. Left out of SECTIONS until that
changes.
"""
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import classify
import storage
from crawler import enforce_filename_component_limit, enforce_folder_component_limit

SITE_TAG = "CSPDCL"
API_BASE = "https://escapi.cspdcl.co.in"

MONTH_NUMBERS = {
    "January": "01", "February": "02", "March": "03", "April": "04",
    "May": "05", "June": "06", "July": "07", "August": "08",
    "September": "09", "October": "10", "November": "11", "December": "12",
}

SECTIONS = {
    "annual_reports": {
        "kind": "flat",
        "list_url": f"{API_BASE}/api/api/investors/getAllAnnualReport",
        "file_url_template": f"{API_BASE}/api/api/investors/AnnualReportfile/{{file_id}}?download=false",
        "id_field": "file_id",
        "title_field": "year",
        "filename_field": "file_name",
        "category": "ANNUAL_REPORTS",
        "subcategory": "GENERAL",
        "found_on": "https://web.cspdcl.co.in/#/AnnualReports",
    },
    "anukampa_orders": {
        "kind": "flat",
        "list_url": f"{API_BASE}/api/api/urjainet/ord-cir/getAnukampaOrders",
        "file_url_template": f"{API_BASE}/api/api/urjainet/ord-cir/downloadDocumentType47File/{{file_id}}",
        "id_field": "ORD_CIR_ID",
        "title_field": "SUBJECT",
        "date_field": "ORDER_DATE",
        "filename_mode": "order_no_date",
        "category": "HR_ORDERS",
        "subcategory": "ANUKAMPA-NIYUKTI",
        "found_on": "https://web.cspdcl.co.in/#/FrmAnukampaNiyukti",
    },
    "medical_orders": {
        "kind": "flat",
        "list_url": f"{API_BASE}/api/api/urjainet/ord-cir/getMedicalOrder",
        "file_url_template": f"{API_BASE}/api/api/urjainet/ord-cir/downloadDocumentType47File/{{file_id}}",
        "id_field": "ORD_CIR_ID",
        "title_field": "SUBJECT",
        "date_field": "ORDER_DATE",
        "filename_mode": "order_no_date",
        "category": "HR_ORDERS",
        "subcategory": "MEDICAL-ORDERS",
        "found_on": "https://web.cspdcl.co.in/#/MedicalOrders",
    },
    "pensioner_orders": {
        "kind": "flat",
        "list_url": f"{API_BASE}/urja/order-circular/public/getPansionerDetails",
        "file_url_template": f"{API_BASE}/urja/order-circular/public/downloadDocumentType47File/{{file_id}}",
        "id_field": "ORD_CIR_ID",
        "title_field": "SUBJECT",
        "date_field": "ORDER_DATE",
        "filename_mode": "order_no_date",
        "category": "PENSIONER",
        "subcategory": "ORDERS",
        "found_on": "https://web.cspdcl.co.in/#/PensionersCorner",
    },
    "recruitment": {
        "kind": "recruitment",
        "pagination_url": f"{API_BASE}/api/api/recruitment/pagination",
        "file_url_template": f"{API_BASE}/api/api/recruitment/getFile/{{file_id}}?download=false",
        "category": "RECRUITMENT",
        "found_on": "https://web.cspdcl.co.in/#/FrmViewRecruitment",
    },
    "tenders": {
        "kind": "tenders",
        "list_url": f"{API_BASE}/urja/tender/public/all-tenders?company=CSPDCL",
        "documents_url_template": f"{API_BASE}/urja/tender/public/tender-documents?tenderId={{tender_id}}&company=CSPDCL",
        "base_url": f"{API_BASE}/urja",
        "category": "TENDER",
        "subcategory": "PROCUREMENT",
        "found_on": "https://web.cspdcl.co.in/#/ViewPublishedTender",
    },
    "saifi_saidi_reports": {
        "kind": "saifi_saidi",
        "years_url": f"{API_BASE}/api/api/saifi-saidi/years",
        "reports_url_template": f"{API_BASE}/api/api/saifi-saidi/reports?year_id={{year_id}}",
        "download_template": f"{API_BASE}/api/api/saifi-saidi/download/{{file_id}}",
        "category": "RELIABILITY_REPORT",
        "subcategory": "SAIFI-SAIDI",
        "found_on": "https://web.cspdcl.co.in/#/SaifiSaidiReports",
    },
    "investor_documents": {
        "kind": "single_file",
        "url": f"{API_BASE}/api/api/investors/document",
        "title": "Preservation of Documents Policy",
        "filename": "preservation-of-documents.pdf",
        "category": "INVESTORS",
        "subcategory": "GENERAL",
        "found_on": "https://web.cspdcl.co.in/#/Investors",
    },
}


def _iso_date_part(iso_str: str | None) -> str | None:
    """'2026-09-01T00:00:00.000Z' -> '2026-09-01'."""
    if not iso_str:
        return None
    return iso_str.split("T", 1)[0]


def _iso_to_ddmmyyyy(iso_str: str | None) -> str | None:
    """'2026-09-01T00:00:00.000Z' -> '01092026' -- matches the real
    filename the backend itself generates for ord-cir downloads
    (confirmed: ORDER_NO=2909, ORDER_DATE=2026-09-01 -> '2909-01092026.pdf'),
    so no HEAD request is needed just to learn the filename."""
    date_part = _iso_date_part(iso_str)
    if not date_part:
        return None
    try:
        y, m, d = date_part.split("-")
        return f"{d}{m}{y}"
    except ValueError:
        return None


def _record_api_document(conn, site_tag: str, min_year: int, folder_map: dict, doc_url: str,
                          found_on: str, title: str, original_filename: str,
                          document_date: str | None, category: str, subcategory: str) -> bool:
    """Same recording logic as crawler._record_document_if_new(), except
    original_filename/document_date come from the API's own JSON fields
    instead of being derived from doc_url -- the file URLs here
    (e.g. .../AnnualReportfile/40?download=false) have no filename or
    extension in them at all, so URL-based derivation would produce a
    useless extensionless name. Returns True if a new row was inserted."""
    existing = storage.get_document(conn, doc_url) or storage.find_by_identity(conn, doc_url, site_tag)
    if existing:
        return False

    year = int(document_date.split("-")[0]) if document_date else None
    if year is not None and year < min_year:
        return False

    file_type = classify.get_file_type(original_filename)
    language = classify.detect_language(doc_url, found_on)
    date_basis_note = "Date taken from the section's own API response, not inferred from filename/URL."
    needs_verification_final = "No" if document_date else "Yes"

    new_filename, target_folder_path, naming_scheme, verification_note = classify.resolve_filename_and_folder(
        site_tag, category, subcategory, title, language, document_date, original_filename,
        doc_url, title, found_on, folder_map,
    )
    new_filename = storage.disambiguate_filename(conn, new_filename, doc_url, site_tag)
    new_filename = enforce_filename_component_limit(new_filename)
    target_folder_path = enforce_folder_component_limit(target_folder_path)
    if verification_note:
        date_basis_note = f"{date_basis_note}; {verification_note}"

    storage.insert_document(conn, {
        "doc_url": doc_url, "site_tag": site_tag, "found_on": found_on,
        "category": category, "subcategory": subcategory, "document_title": title,
        "original_filename": original_filename, "file_type": file_type, "language": language,
        "document_date": document_date, "date_basis_note": date_basis_note,
        "new_filename": new_filename, "target_folder_path": target_folder_path,
        "needs_verification": needs_verification_final, "naming_scheme": naming_scheme,
    })
    return True


def _discover_flat(conn, cfg, section: dict) -> tuple[int, int]:
    """A single GET returning {"data": [...]} -- covers Annual Reports and
    every ord-cir-style order/circular list (Anukampa, Medical, Pensioner)."""
    resp = requests.get(section["list_url"], timeout=20)
    resp.raise_for_status()
    items = resp.json().get("data", [])

    new_count = 0
    for item in items:
        file_id = item[section["id_field"]]
        doc_url = section["file_url_template"].format(file_id=file_id)
        title = str(item.get(section["title_field"], "")).strip()

        if section.get("filename_mode") == "order_no_date":
            order_no = str(item.get("ORDER_NO", file_id))
            ddmmyyyy = _iso_to_ddmmyyyy(item.get(section["date_field"]))
            original_filename = f"{order_no}-{ddmmyyyy}.pdf" if ddmmyyyy else f"{order_no}.pdf"
        else:
            raw_filename = str(item.get(section["filename_field"]) or f"document_{file_id}")
            original_filename = raw_filename if raw_filename.lower().endswith(".pdf") else f"{raw_filename}.pdf"

        if section.get("date_field"):
            document_date = _iso_date_part(item.get(section["date_field"]))
        else:
            # Annual Reports' "year" title (e.g. "Year 2024-2025") carries
            # the only date signal available -- no separate date field.
            year_match = re.search(r"(\d{4})", title)
            document_date = f"{year_match.group(1)}-01-01" if year_match else None

        if _record_api_document(conn, cfg.site_tag, cfg.min_year, cfg.folder_map, doc_url,
                                 section["found_on"], title, original_filename, document_date,
                                 section["category"], section["subcategory"]):
            new_count += 1
    return new_count, len(items)


def _discover_recruitment(conn, cfg, section: dict) -> tuple[int, int]:
    """Paginated (page/limit=10), each post nested post -> particulars ->
    flags, where each flag is one real file (SRNO=file id,
    FILE_NAME="Label|actual_filename.pdf")."""
    new_count = 0
    total_count = 0
    page = 1
    while True:
        resp = requests.get(section["pagination_url"], params={"page": page, "limit": 10}, timeout=20)
        resp.raise_for_status()
        payload = resp.json()
        total_pages = payload.get("totalPages", page)

        for post in payload.get("data", []):
            post_name = str(post.get("NAME_OF_POST", "")).strip()
            for particular in post.get("particulars") or []:
                desc = str(particular.get("PARTICULARS_DESC", "")).strip() or "General"
                subcategory = re.sub(r"[^A-Z0-9]+", "-", desc.upper()).strip("-") or "GENERAL"
                for flag in particular.get("flags") or []:
                    file_id = flag.get("SRNO")
                    if file_id is None:
                        continue
                    total_count += 1
                    raw_name = str(flag.get("FILE_NAME") or f"document_{file_id}")
                    label, _, original_filename = raw_name.partition("|")
                    if not original_filename:
                        label, original_filename = desc, raw_name
                    if not original_filename.lower().endswith(".pdf"):
                        original_filename = f"{original_filename}.pdf"
                    doc_url = section["file_url_template"].format(file_id=file_id)
                    title = " - ".join(p for p in (post_name, desc, label.strip()) if p)

                    if _record_api_document(conn, cfg.site_tag, cfg.min_year, cfg.folder_map, doc_url,
                                             section["found_on"], title, original_filename, None,
                                             section["category"], subcategory):
                        new_count += 1

        if page >= total_pages:
            break
        page += 1
    return new_count, total_count


def _discover_tenders(conn, cfg, section: dict) -> tuple[int, int]:
    """Two-step: one call lists every tender (id, notice no, dates, no
    documents), then one call per tender id returns its real documents.
    ~1,300 tenders -> ~1,300 follow-up calls, so these run concurrently;
    DB writes stay on the main thread as each future completes."""
    resp = requests.get(section["list_url"], timeout=30)
    resp.raise_for_status()
    tenders = resp.json().get("data", [])

    def fetch_documents(tender: dict) -> list:
        tender_id = tender.get("id")
        if not tender_id:
            return []
        try:
            r = requests.get(section["documents_url_template"].format(tender_id=tender_id), timeout=15)
            r.raise_for_status()
            return r.json().get("data", []) or []
        except requests.RequestException:
            return []

    new_count = 0
    total_count = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_documents, tender): tender for tender in tenders}
        for future in as_completed(futures):
            tender = futures[future]
            for doc in future.result():
                download_path = doc.get("downloadUrl")
                if not download_path:
                    continue
                total_count += 1
                doc_url = section["base_url"] + download_path
                title = " - ".join(
                    p for p in (str(tender.get("tenderNoticeNo", "")).strip(),
                                str(doc.get("displayName", "")).strip()) if p
                )
                original_filename = str(doc.get("fileName") or f"document_{doc.get('docId')}.pdf")
                if not original_filename.lower().endswith(".pdf"):
                    original_filename = f"{original_filename}.pdf"
                document_date = _iso_date_part(tender.get("uploadedAt"))

                if _record_api_document(conn, cfg.site_tag, cfg.min_year, cfg.folder_map, doc_url,
                                         section["found_on"], title, original_filename, document_date,
                                         section["category"], section["subcategory"]):
                    new_count += 1
    return new_count, total_count


def _discover_saifi_saidi(conn, cfg, section: dict) -> tuple[int, int]:
    """Two-step: years list, then one reports-per-year call giving one
    file per month. Real filename is a fixed, derivable pattern
    (confirmed via Content-Disposition: 'SAIFI_SAIDI_Report_2024_January.xlsx')
    so no extra HEAD request is needed."""
    resp = requests.get(section["years_url"], timeout=20)
    resp.raise_for_status()
    years = resp.json().get("data", [])

    new_count = 0
    total_count = 0
    for year_entry in years:
        year_id = year_entry.get("year_id")
        year = year_entry.get("year")
        if year_id is None:
            continue
        r = requests.get(section["reports_url_template"].format(year_id=year_id), timeout=20)
        if r.status_code != 200:
            continue
        for month in r.json().get("data", []) or []:
            file_details = month.get("file_details") or {}
            if not file_details.get("has_file"):
                continue
            total_count += 1
            file_id = file_details.get("file_id")
            month_name = month.get("month_name", "")
            doc_url = section["download_template"].format(file_id=file_id)
            title = f"SAIFI SAIDI Report - {year} {month_name}".strip()
            original_filename = f"SAIFI_SAIDI_Report_{year}_{month_name}.xlsx"
            month_num = MONTH_NUMBERS.get(month_name)
            document_date = f"{year}-{month_num}-01" if year and month_num else None

            if _record_api_document(conn, cfg.site_tag, cfg.min_year, cfg.folder_map, doc_url,
                                     section["found_on"], title, original_filename, document_date,
                                     section["category"], section["subcategory"]):
                new_count += 1
    return new_count, total_count


def _discover_single_file(conn, cfg, section: dict) -> tuple[int, int]:
    """A page with exactly one static document behind its download button
    (e.g. Investors' "Preservation of Documents" policy) -- no list API."""
    is_new = _record_api_document(conn, cfg.site_tag, cfg.min_year, cfg.folder_map,
                                   section["url"], section["found_on"], section["title"],
                                   section["filename"], None, section["category"], section["subcategory"])
    return (1 if is_new else 0), 1


_KIND_HANDLERS = {
    "flat": _discover_flat,
    "recruitment": _discover_recruitment,
    "tenders": _discover_tenders,
    "saifi_saidi": _discover_saifi_saidi,
    "single_file": _discover_single_file,
}


def discover_section(conn, cfg, section_key: str) -> tuple[int, int]:
    """Calls one section's listing API/APIs and records every document it
    returns that isn't already known. Returns (new_count, total_count)."""
    section = SECTIONS[section_key]
    handler = _KIND_HANDLERS[section.get("kind", "flat")]
    return handler(conn, cfg, section)


def discover_all(conn, cfg) -> dict[str, tuple[int, int]]:
    """Runs every known section for this site. Returns {section_key: (new, total)}."""
    return {key: discover_section(conn, cfg, key) for key in SECTIONS}


if __name__ == "__main__":
    import json as _json

    import crawler

    cfg = crawler.load_site_config(crawler.resolve_config_path(SITE_TAG.lower()))
    conn = storage.get_connection()
    try:
        results = discover_all(conn, cfg)
        conn.commit()
    finally:
        conn.close()
    print(_json.dumps({k: {"new": n, "total": t} for k, (n, t) in results.items()}, indent=2))
