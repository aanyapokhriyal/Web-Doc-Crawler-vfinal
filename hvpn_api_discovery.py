"""HVPN (hvpn.org.in)-specific document discovery via its backend REST API.

HVPN is an Angular SPA (`<app-root>`) whose real content -- including every
document -- is loaded from a separate backend on a non-default port,
`https://hvpn.org.in:8443`, never from the crawlable page itself. A normal
crawl only ever finds whatever direct links happen to already be rendered
into the first page of a handful of hash routes (confirmed: a full run
visited just 11 pages and found 51 documents, out of what turns out to be
several thousand).

Found by downloading the site's own `main.<hash>.js` bundle with plain
`requests` and grepping it for `hvpn.org.in:8443/api/...` route literals.
No API key or auth is needed for any of these -- they're plain public reads
-- but the server's TLS cert on port 8443 doesn't validate, so every call
here uses `verify=False` (confirmed: `requests` raises `SSLError` without
it; this is a cert misconfiguration on HVPN's own server, not a security
concern for reading already-public documents).

Two distinct API shapes, so two discovery strategies, both built on
existing `crawler.py` machinery instead of reimplementing it:

1. Eleven flat list endpoints (`/api/users/get<name>`) where each item
   already has a ready-to-use `address` field -- the direct document URL.
   Recorded via `crawler._record_document_if_new()` directly (the exact
   function `crawl_site()` itself uses), so classification, dedup,
   filename/folder resolution, and date extraction all stay consistent
   with every other site.
2. `/api/getsinglepage/<slug>` (73 known slugs, one per site section) and
   `/api/users/getnews`, where the real payload is a raw HTML blob in a
   `content` field (e.g. `<a href="https://hvpn.org.in:8443/profile/...">`)
   -- the exact same shape as an ordinarily-crawled page's HTML, so it's
   run through `crawler.extract_links()` + `crawler.is_document_link()`
   exactly as a real page would be, then also `_record_document_if_new()`.
"""
import warnings

import requests

import storage
from crawler import CrawlStats, _record_document_if_new, extract_links, is_document_link

warnings.filterwarnings("ignore", message="Unverified HTTPS request")

SITE_TAG = "HVPN"
BASE = "https://hvpn.org.in"
API_BASE = "https://hvpn.org.in:8443/api"

FLAT_ENDPOINTS = {
    "tenders": {
        "endpoint": "gettender", "title_fields": ["nit_number", "description"],
        "category": "TENDER", "subcategory": "PROCUREMENT", "found_on": f"{BASE}/#/tender",
    },
    "office_orders": {
        "endpoint": "getofficeorder", "title_fields": ["topic", "memo"],
        "category": "CIRCULAR", "subcategory": "OFFICE-ORDERS", "found_on": f"{BASE}/#/updates",
    },
    "policies": {
        "endpoint": "getpolicies", "title_fields": ["topic", "name"],
        "category": "CIRCULAR", "subcategory": "POLICIES", "found_on": f"{BASE}/#/updates",
    },
    "rti_circulars": {
        "endpoint": "getrticirculars", "title_fields": ["title"],
        "category": "RTI", "subcategory": "CIRCULARS", "found_on": f"{BASE}/#/rti",
    },
    "rti_faa": {
        "endpoint": "getrtifaa", "title_fields": ["title"],
        "category": "RTI", "subcategory": "FAA-ORDERS", "found_on": f"{BASE}/#/rti",
    },
    "acts": {
        "endpoint": "getacts", "title_fields": ["name", "topic"],
        "category": "ACT", "subcategory": "GENERAL", "found_on": f"{BASE}/#/acts",
    },
    "seniority_lists": {
        "endpoint": "getseniority", "title_fields": ["title", "designation"],
        "category": "SENIORITY-LIST", "subcategory": "GENERAL", "found_on": f"{BASE}/#/employees",
    },
    "downloads": {
        "endpoint": "gethvpndownload", "title_fields": ["description", "topic", "subtopic"],
        "category": "DOWNLOADS", "subcategory": "GENERAL", "found_on": f"{BASE}/#/hvpndocs",
    },
    "instructions": {
        "endpoint": "getinstructions", "title_fields": ["description", "topic", "subtopic"],
        "category": "CIRCULAR", "subcategory": "INSTRUCTIONS", "found_on": f"{BASE}/#/updates",
    },
    "combined": {
        "endpoint": "getcombined", "title_fields": ["name"],
        "category": "TENDER", "subcategory": "COMBINED", "found_on": f"{BASE}/#/tender",
    },
}

# All 73 known section slugs for /api/getsinglepage/<slug>, found via a
# bundle grep (see module docstring) -- each returns a list of {content}
# items whose content is a raw HTML blob to run through extract_links().
SINGLEPAGE_SLUGS = [
    "23rd_celebration", "7th", "AFR", "ATC_TTC", "Admin_HRD", "Admin_Others",
    "Board", "Budget", "CSR", "Circulars_Policies", "Company_Overview",
    "Company_Secretary", "Compendium", "Contact_Us-Field_Offices",
    "Contact_Us-Head_Office", "Corporate_Governance", "Court_Cases",
    "Cyber_Security", "Deviation_Settlement_Mechanism", "Download",
    "EPC_contracts", "Energy_Accounting", "Equipments", "Funding_Status",
    "HPTI-Knowledge_bank", "HPTI-Previous", "HPTI-Regulations", "HPTI-Results",
    "IT-initiatives", "Incumbency_Officers", "Material_Management",
    "Medical-Bills", "Medical-Payments", "Medical_Reimbursement", "More",
    "Objectives", "Online", "Open_Access_Customer_Application_Status",
    "Open_Access_Detail_Procedures", "Open_Access_HERC_Regulations",
    "Open_Access_NOAR", "Organisation", "Performas", "Petitions", "Planning",
    "Public", "RTI_Applications_and_Appeals", "RTI_designated_Officers",
    "RTI_disclosures", "RTI_monthly_abstract",
    "Reactive_Energy_Charges_Pool_Fund", "Recruitment_Career", "Regulatory",
    "SEBI2015", "SOPs", "Solar_Projects", "TSA_Calculations",
    "Technical_specification", "Ticketing_tool", "Transmission_Losses",
    "Transmission_System", "Transmission_System_Availability", "Vigilance",
    "contactus", "inv_corpannouncement", "inv_disc_sebi", "inv_services",
    "mainMenu", "purchase_orders", "sitemap", "testingcomponent", "web_links",
    "welcome_banner",
]


def _build_title(item: dict, fields: list) -> str:
    parts = [str(item.get(f, "")).strip() for f in fields if item.get(f)]
    return " - ".join(parts) or "Untitled"


def discover_flat_endpoint(conn, cfg, stats: CrawlStats, key: str) -> tuple[int, int]:
    section = FLAT_ENDPOINTS[key]
    resp = requests.get(f"{API_BASE}/users/{section['endpoint']}", timeout=30, verify=False)
    resp.raise_for_status()
    items = resp.json()

    before = stats.documents_new
    for item in items:
        address = item.get("address")
        if not address:
            continue
        title = _build_title(item, section["title_fields"])
        _record_document_if_new(conn, cfg, stats, address, section["found_on"],
                                 section["category"], section["subcategory"], title, cfg.keyword_rules)
    return stats.documents_new - before, len(items)


def discover_singlepage(conn, cfg, stats: CrawlStats, slug: str) -> tuple[int, int]:
    found_on = f"{BASE}/#/{slug}"
    resp = requests.get(f"{API_BASE}/getsinglepage/{slug}", timeout=30, verify=False)
    resp.raise_for_status()
    items = resp.json()

    before = stats.documents_new
    total = 0
    for item in items:
        html = item.get("content") or ""
        for link_url, link_text in extract_links(html, found_on):
            if is_document_link(link_url):
                total += 1
                _record_document_if_new(conn, cfg, stats, link_url, found_on,
                                         "MISC", "GENERAL", link_text, cfg.keyword_rules)
    return stats.documents_new - before, total


def discover_news(conn, cfg, stats: CrawlStats) -> tuple[int, int]:
    """getnews has the same embedded-HTML shape as getsinglepage, but lives
    under /api/users/ instead -- its own function so SECTIONS below doesn't
    need a third, redundant "kind"."""
    found_on = f"{BASE}/#/news"
    resp = requests.get(f"{API_BASE}/users/getnews", timeout=30, verify=False)
    resp.raise_for_status()
    items = resp.json()

    before = stats.documents_new
    total = 0
    for item in items:
        html = item.get("content") or ""
        for link_url, link_text in extract_links(html, found_on):
            if is_document_link(link_url):
                total += 1
                title = link_text or str(item.get("newsname", ""))
                _record_document_if_new(conn, cfg, stats, link_url, found_on,
                                         "MISC", "GENERAL", title, cfg.keyword_rules)
    return stats.documents_new - before, total


def discover_all(conn, cfg) -> dict[str, tuple[int, int]]:
    """Runs every known endpoint for this site. Returns {key: (new, total)}."""
    stats = CrawlStats()
    results = {}
    for key in FLAT_ENDPOINTS:
        results[key] = discover_flat_endpoint(conn, cfg, stats, key)
    results["news"] = discover_news(conn, cfg, stats)
    for slug in SINGLEPAGE_SLUGS:
        results[f"page:{slug}"] = discover_singlepage(conn, cfg, stats, slug)
    return results


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
