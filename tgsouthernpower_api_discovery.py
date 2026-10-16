"""TGSOUTHERNPOWER (tgsouthernpower.org)-specific document discovery via its
backend REST API.

Same root problem as CSPDCL: every document "download" is a plain
`<button onClick=...>` with no href at all -- invisible to any HTML/link-
based crawling (confirmed via real HTML the user pasted:
`<button class="ar-icon-btn ar-icon-dl" title="Download 25th Annual Report
2024-25.pdf">`). This module calls the real backend directly instead.

Found by downloading the site's own `/assets/index-<hash>.js` bundle with
plain `requests` (the site has an active WAF that blocks Playwright/browser
traffic -- "The URL you requested has been blocked" -- but plain `requests`
passes fine for both the page shell and this bundle) and grepping it for
`/api/...` route literals, then for the frontend's own hardcoded public API
key (X-API-Key, shipped in the bundle -- it's not a secret, every visitor's
browser already sends it): var Ir equals the literal key string, used as
function G(e={}){return{"X-API-Key":Ir,...e}}.

Confirmed working (plain `requests`, this key, no browser needed):
    GET  /api/download-forms                         -> sections[].items[]
    GET  /api/tariff/schedules                        -> list of items
    GET  /api/tariff/orders                            -> list of items
    GET  /api/tariff/earlier                           -> list of items
    GET  /api/tariff/cost-data                         -> list of items
    GET  /api/<same path>/<id>/document                -> the actual file

Follow-up pass (same session, after the user reported the count still looked
too low): the first pass completely missed Tenders -- by far the largest
category on the site. There are actually 4 separate tender archives, not
one, each a distinct API path under `/api/public/tenders/...` found the
same way (bundle route-literal grep, then confirmed live):
    /api/public/tenders                        -- current/open tenders (44)
    /api/public/tenders/general-old             -- old general tenders (1,804)
    /api/public/tenders/pmm-tender-notices       -- PMM notices (2)
    /api/public/tenders/pmm-tender-specifications?page=&size= -- PMM specs,
        paginated (213 across 22 pages of 10)
Each tender can also carry its own amendments[]/extensions[] sub-documents;
confirmed download URL patterns for the "current" and "pmm-tender-specifications"
archives (`.../{id}/amendments/{amendmentNo}/document` etc.) -- not confirmed
for the other two (their sample amendments/extensions arrays were empty and no
matching URL literal was found in the bundle), so those two only record the
main per-tender document.

Also added: Achievements (`/api/achievements` -- a plain array of filenames,
served as static files under `/uploads/documents/pdf/achievements/<name>`,
no per-item API call at all), Careers (`/api/public/careers`, one PDF per
notification), and Announcements (`/api/public/announcements`, rarely
carries a document but the field is there when it does).

Known gap, NOT fixed here: Annual Reports, Quarterly Reports, and
Performance Summary all resolve to `/api/cms/financial-reports` and
`/api/cms/performance-summary` -- but unlike every endpoint above, those
return HTTP 403 "invalid or missing API key" even WITH the same key. That
strongly indicates a genuinely separate, admin-only credential gates the
`/api/cms/*` namespace (its own UI has Edit/Delete buttons -- it's the CMS
admin panel, not a public reader). Guessing or brute-forcing that second
credential is out of scope; left out of SECTIONS until a legitimate public
endpoint for these three sections is found.
"""
import re

import requests

import classify
import storage
from crawler import enforce_filename_component_limit, enforce_folder_component_limit

SITE_TAG = "TGSOUTHERNPOWER"
BASE = "https://tgsouthernpower.org"
API_KEY = "TGSWpDcL2025XkNpMqRvFzAcYeUhBsJ3"
HEADERS = {"X-API-Key": API_KEY, "User-Agent": "Mozilla/5.0"}

SECTIONS = {
    "download_forms": {
        "kind": "download_forms",
        "list_url": f"{BASE}/api/download-forms",
        "file_url_template": f"{BASE}/api/download-forms/{{item_id}}/document",
        "category": "FORMS",
        "found_on": f"{BASE}/#eodb",
    },
    "tariff_schedules": {
        "kind": "tariff",
        "list_url": f"{BASE}/api/tariff/schedules",
        "file_url_template": f"{BASE}/api/tariff/schedules/{{item_id}}/document",
        "category": "TARIFF",
        "subcategory": "SCHEDULES",
        "found_on": f"{BASE}/#tariff-schedules",
    },
    "tariff_orders": {
        "kind": "tariff",
        "list_url": f"{BASE}/api/tariff/orders",
        "file_url_template": f"{BASE}/api/tariff/orders/{{item_id}}/document",
        "category": "TARIFF",
        "subcategory": "ORDERS",
        "found_on": f"{BASE}/#tariff-orders",
    },
    "tariff_earlier": {
        "kind": "tariff",
        "list_url": f"{BASE}/api/tariff/earlier",
        "file_url_template": f"{BASE}/api/tariff/earlier/{{item_id}}/document",
        "category": "TARIFF",
        "subcategory": "EARLIER",
        "found_on": f"{BASE}/#tariff-earlier",
    },
    "tariff_cost_data": {
        "kind": "cost_data",
        "list_url": f"{BASE}/api/tariff/cost-data",
        "file_url_template": f"{BASE}/api/tariff/cost-data/{{item_id}}/document",
        "category": "TARIFF",
        "subcategory": "COST-DATA",
        "found_on": f"{BASE}/#cost-data",
    },
    "tenders_current": {
        "kind": "tenders_flat",
        "list_url": f"{BASE}/api/public/tenders",
        "doc_template": f"{BASE}/api/public/tenders/{{tender_id}}/document",
        "amendment_template": f"{BASE}/api/public/tenders/{{tender_id}}/amendments/{{num}}/document",
        "extension_template": f"{BASE}/api/public/tenders/{{tender_id}}/extensions/{{num}}/document",
        "date_field": "publishedDate",
        "category": "TENDER",
        "subcategory": "CURRENT",
        "found_on": f"{BASE}/#tenders",
    },
    "tenders_general_old": {
        "kind": "tenders_flat",
        "list_url": f"{BASE}/api/public/tenders/general-old",
        "doc_template": f"{BASE}/api/public/tenders/general-old/{{tender_id}}/document",
        "date_field": "entryDate",
        "category": "TENDER",
        "subcategory": "GENERAL-OLD",
        "found_on": f"{BASE}/#tenders",
    },
    "tenders_pmm_notices": {
        "kind": "tenders_flat",
        "list_url": f"{BASE}/api/public/tenders/pmm-tender-notices",
        "doc_template": f"{BASE}/api/public/tenders/pmm-tender-notices/{{tender_id}}/document",
        "date_field": "closingDate",
        "category": "TENDER",
        "subcategory": "PMM-NOTICES",
        "found_on": f"{BASE}/#tenders",
    },
    "tenders_pmm_specs": {
        "kind": "tenders_paginated",
        "list_url": f"{BASE}/api/public/tenders/pmm-tender-specifications",
        "doc_template": f"{BASE}/api/public/tenders/pmm-tender-specifications/{{tender_id}}/document",
        "amendment_template": f"{BASE}/api/public/tenders/pmm-tender-specifications/{{tender_id}}/amendments/{{num}}/document",
        "extension_template": f"{BASE}/api/public/tenders/pmm-tender-specifications/{{tender_id}}/extensions/{{num}}/document",
        "date_field": "entryDate",
        "category": "TENDER",
        "subcategory": "PMM-SPECIFICATIONS",
        "found_on": f"{BASE}/#tenders",
    },
    "achievements": {
        "kind": "static_files",
        "list_url": f"{BASE}/api/achievements",
        "base_path": f"{BASE}/uploads/documents/pdf/achievements",
        "category": "ACHIEVEMENTS",
        "subcategory": "GENERAL",
        "found_on": f"{BASE}/#achievements",
    },
    "careers": {
        "kind": "flat_id_date",
        "list_url": f"{BASE}/api/public/careers",
        "file_url_template": f"{BASE}/api/public/careers/{{item_id}}/document",
        "id_field": "id",
        "title_field": "description",
        "date_field": "entryDate",
        "category": "CAREERS",
        "subcategory": "GENERAL",
        "found_on": f"{BASE}/#careers",
    },
    "announcements": {
        "kind": "flat_id_date",
        "list_url": f"{BASE}/api/public/announcements",
        "file_url_template": f"{BASE}/api/public/announcements/{{item_id}}/document",
        "id_field": "id",
        "title_field": "title",
        "date_field": None,
        "category": "ANNOUNCEMENTS",
        "subcategory": "GENERAL",
        "found_on": f"{BASE}/#home",
    },
}


def _record_api_document(conn, site_tag: str, min_year: int, folder_map: dict, doc_url: str,
                          found_on: str, title: str, original_filename: str,
                          document_date: str | None, category: str, subcategory: str) -> bool:
    """Same shape as cspdcl_api_discovery._record_api_document() -- the file
    URLs here (.../schedules/69/document) have no filename/extension in
    them at all, so the filename has to come from the API's own JSON
    fields instead of being derived from the URL. Returns True if a new
    row was inserted."""
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


def _finyear_to_date(finyear: str | None) -> str | None:
    """'2026-27' -> '2026-01-01' -- same approximation used for CSPDCL's
    Annual Reports (only a year range is available, not an exact date)."""
    if not finyear:
        return None
    match = re.search(r"(\d{4})", finyear)
    return f"{match.group(1)}-01-01" if match else None


def _discover_download_forms(conn, cfg, section: dict) -> tuple[int, int]:
    """One GET returning sections, each with nested items -- no per-item
    date available (these are static reference forms, not dated filings)."""
    resp = requests.get(section["list_url"], headers=HEADERS, timeout=20)
    resp.raise_for_status()
    sections = resp.json()

    new_count = 0
    total_count = 0
    for sect in sections:
        section_name = str(sect.get("sectionName", "")).strip() or "General"
        subcategory = re.sub(r"[^A-Z0-9]+", "-", section_name.upper()).strip("-") or "GENERAL"
        for item in sect.get("items", []) or []:
            if not item.get("hasDocument"):
                continue
            total_count += 1
            item_id = item.get("id")
            doc_url = section["file_url_template"].format(item_id=item_id)
            title = str(item.get("label", "")).strip()
            original_filename = f"{title or f'form_{item_id}'}.pdf"

            if _record_api_document(conn, cfg.site_tag, cfg.min_year, cfg.folder_map, doc_url,
                                     section["found_on"], title, original_filename, None,
                                     section["category"], subcategory):
                new_count += 1
    return new_count, total_count


def _discover_tariff(conn, cfg, section: dict) -> tuple[int, int]:
    """Flat list keyed by ukTariff; real filename is
    '{description}.{docExt}' -- confirmed exactly against the frontend's
    own download handler, so no extra request is needed to learn it."""
    resp = requests.get(section["list_url"], headers=HEADERS, timeout=20)
    resp.raise_for_status()
    items = resp.json()

    new_count = 0
    for item in items:
        item_id = item.get("ukTariff")
        doc_url = section["file_url_template"].format(item_id=item_id)
        title = str(item.get("description", "")).strip()
        ext = item.get("docExt") or "pdf"
        original_filename = f"{title or f'tariff_{item_id}'}.{ext}"
        document_date = _finyear_to_date(item.get("finYear"))

        if _record_api_document(conn, cfg.site_tag, cfg.min_year, cfg.folder_map, doc_url,
                                 section["found_on"], title, original_filename, document_date,
                                 section["category"], section["subcategory"]):
            new_count += 1
    return new_count, len(items)


def _discover_cost_data(conn, cfg, section: dict) -> tuple[int, int]:
    """Flat list keyed by reportId; unlike tariff items this one gives an
    exact publishDate and a ready-made docName, no derivation needed."""
    resp = requests.get(section["list_url"], headers=HEADERS, timeout=20)
    resp.raise_for_status()
    items = resp.json()

    new_count = 0
    for item in items:
        item_id = item.get("reportId")
        doc_url = section["file_url_template"].format(item_id=item_id)
        title = str(item.get("reportDesc", "")).strip()
        original_filename = str(item.get("docName") or f"cost_data_{item_id}.pdf")
        document_date = item.get("publishDate")

        if _record_api_document(conn, cfg.site_tag, cfg.min_year, cfg.folder_map, doc_url,
                                 section["found_on"], title, original_filename, document_date,
                                 section["category"], section["subcategory"]):
            new_count += 1
    return new_count, len(items)


def _strip_html(text: str | None) -> str:
    """Several tender fields (title, description, specNo) come back as raw
    HTML fragments (e.g. '<p>TENDER SPECIFICATION STN-749/21</p>') -- strip
    tags and collapse whitespace for a usable title."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def _normalize_date(raw) -> str | None:
    """Tender date fields show up as '2026-10-12', '2026-09-19 16:31:03.0',
    or '2016-07-20T12:00' -- all start with a plain YYYY-MM-DD, so a prefix
    match covers every case actually seen without needing per-field parsing."""
    if not raw:
        return None
    match = re.match(r"(\d{4}-\d{2}-\d{2})", str(raw))
    return match.group(1) if match else None


def _record_tender_item(conn, cfg, section: dict, tender_id, title: str,
                         document_date: str | None, doc_ext: str | None,
                         doc_url: str, suffix: str = "") -> bool:
    ext = (doc_ext or "pdf").lstrip(".")
    label = title or f"Tender {tender_id}"
    original_filename = f"{label}{suffix}.{ext}"
    return _record_api_document(conn, cfg.site_tag, cfg.min_year, cfg.folder_map, doc_url,
                                 section["found_on"], f"{label}{suffix}", original_filename,
                                 document_date, section["category"], section["subcategory"])


def _record_tender_and_subdocs(conn, cfg, section: dict, item: dict) -> tuple[int, int]:
    """Records a tender's main document plus, when this section's config
    defines the URL templates for them, its amendments/extensions as
    separate documents -- each is a real, independently-downloadable file.
    Returns (new_count, total_count) counting every sub-document attempted,
    not just the one main tender record."""
    new_count = 0
    total_count = 0
    tender_id = item.get("tenderId")
    if tender_id is None:
        return 0, 0
    title = _strip_html(item.get("title") or item.get("description") or item.get("specNo"))
    document_date = _normalize_date(item.get(section["date_field"]))
    doc_ext = item.get("docExt")

    if item.get("hasDocument"):
        total_count += 1
        doc_url = section["doc_template"].format(tender_id=tender_id)
        if _record_tender_item(conn, cfg, section, tender_id, title, document_date, doc_ext, doc_url):
            new_count += 1

    if "amendment_template" in section:
        for amend in item.get("amendments") or []:
            if not amend.get("hasDocument"):
                continue
            total_count += 1
            num = amend.get("amendmentNo")
            doc_url = section["amendment_template"].format(tender_id=tender_id, num=num)
            if _record_tender_item(conn, cfg, section, tender_id, title, document_date,
                                    amend.get("docExt") or doc_ext, doc_url, suffix=f" - Amendment {num}"):
                new_count += 1

    if "extension_template" in section:
        for ext_item in item.get("extensions") or []:
            if not ext_item.get("hasDocument"):
                continue
            total_count += 1
            num = ext_item.get("extensionNo")
            doc_url = section["extension_template"].format(tender_id=tender_id, num=num)
            if _record_tender_item(conn, cfg, section, tender_id, title, document_date,
                                    ext_item.get("docExt") or doc_ext, doc_url, suffix=f" - Extension {num}"):
                new_count += 1

    return new_count, total_count


def _discover_tenders_flat(conn, cfg, section: dict) -> tuple[int, int]:
    resp = requests.get(section["list_url"], headers=HEADERS, timeout=30)
    resp.raise_for_status()
    items = resp.json()

    new_count = 0
    total_count = 0
    for item in items:
        n, t = _record_tender_and_subdocs(conn, cfg, section, item)
        new_count += n
        total_count += t
    return new_count, total_count


def _discover_tenders_paginated(conn, cfg, section: dict) -> tuple[int, int]:
    new_count = 0
    total_count = 0
    page = 1
    while True:
        resp = requests.get(section["list_url"], headers=HEADERS,
                             params={"page": page, "size": 10}, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        content = payload.get("content", [])
        for item in content:
            n, t = _record_tender_and_subdocs(conn, cfg, section, item)
            new_count += n
            total_count += t

        total_pages = payload.get("totalPages", page)
        if page >= total_pages or not content:
            break
        page += 1
    return new_count, total_count


def _discover_static_files(conn, cfg, section: dict) -> tuple[int, int]:
    """achievements: a plain array of filenames served as static files --
    no id, no date, just the filename itself."""
    resp = requests.get(section["list_url"], headers=HEADERS, timeout=20)
    resp.raise_for_status()
    filenames = resp.json()

    new_count = 0
    for filename in filenames:
        doc_url = f"{section['base_path']}/{filename}"
        title = re.sub(r"[-_]+", " ", re.sub(r"\.pdf$", "", filename, flags=re.IGNORECASE)).strip().title()

        if _record_api_document(conn, cfg.site_tag, cfg.min_year, cfg.folder_map, doc_url,
                                 section["found_on"], title, filename, None,
                                 section["category"], section["subcategory"]):
            new_count += 1
    return new_count, len(filenames)


def _discover_flat_id_date(conn, cfg, section: dict) -> tuple[int, int]:
    """careers / announcements: a flat list of {id, <title field>,
    hasDocument, <date field>}."""
    resp = requests.get(section["list_url"], headers=HEADERS, timeout=30)
    resp.raise_for_status()
    items = resp.json()

    new_count = 0
    for item in items:
        if not item.get("hasDocument"):
            continue
        item_id = item.get(section["id_field"])
        doc_url = section["file_url_template"].format(item_id=item_id)
        title = _strip_html(str(item.get(section["title_field"], "")))
        original_filename = f"{title or f'document_{item_id}'}.pdf"
        document_date = _normalize_date(item.get(section["date_field"])) if section.get("date_field") else None

        if _record_api_document(conn, cfg.site_tag, cfg.min_year, cfg.folder_map, doc_url,
                                 section["found_on"], title, original_filename, document_date,
                                 section["category"], section["subcategory"]):
            new_count += 1
    return new_count, len(items)


_KIND_HANDLERS = {
    "download_forms": _discover_download_forms,
    "tariff": _discover_tariff,
    "cost_data": _discover_cost_data,
    "tenders_flat": _discover_tenders_flat,
    "tenders_paginated": _discover_tenders_paginated,
    "static_files": _discover_static_files,
    "flat_id_date": _discover_flat_id_date,
}


def discover_section(conn, cfg, section_key: str) -> tuple[int, int]:
    section = SECTIONS[section_key]
    handler = _KIND_HANDLERS[section["kind"]]
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
