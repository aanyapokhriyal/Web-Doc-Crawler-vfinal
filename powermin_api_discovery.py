"""POWERMIN (powermin.gov.in)-specific document discovery via its backend
WordPress REST API.

powermin.gov.in was rebuilt since its last crawl (191 docs, 2026-08-28) into
a Next.js frontend backed by a headless WordPress CMS at
`www.powermin.gov.in/cms/wp-json/...` (a reverse proxy in front of the real
backend, `cms-powermin.digifootprint.gov.in` -- a shared CMS platform; its
sitemap.xml even leaks another ministry's URLs, confirming multi-tenancy).
Every call needs a public `apikey` header found hardcoded in the site's own
JS bundle -- not a secret, every visitor's browser already sends it.

Two separate, non-overlapping document systems were found, investigated by
downloading the site's Next.js chunks with plain `requests` and grepping
them for `wp-json/...` route literals (Playwright is blocked by this site's
WAF -- "Access Denied" -- confirmed, not retried):

1. `central_documents` (a WP custom post type, ~1,827 posts) -- the BIG one.
   Each post's ACF fields resolve cleanly via `acf_format=standard`: a
   `file_date`, and up to 3 already-resolved file objects (`pdf`,
   `pdf_hindi`, `pdf_both`) each with a real `url`. No per-post extra
   request needed. THE key that unlocked this: `acf_format=standard` only
   resolves a *top-level* ACF File field into a full object -- which is
   exactly what `central_documents` uses.

2. `documents` (a different WP custom post type, 71 posts across 16
   `documents_category` taxonomy terms -- confirmed via
   `wp-json/document/documents?document_category=<slug>` matching the
   user's own examples exactly: "Summary of Monthly Accounts" = 170 files,
   "Rules Framed under Electricity Act 2003" = 77, "New Notices/Orders" =
   145). BUT: each bundled file lives in a *repeater* field (`file[].file`)
   -- an array of objects, each holding a File field -- and ACF's REST API
   does not resolve File fields nested inside a Repeater the way it does a
   top-level one. Exhaustively tried: `acf_format=standard` (alone, and
   combined with `_fields=acf`) on every endpoint that returns this data,
   direct `/wp/v2/media/<id>` lookups (always "Invalid post ID"), paginating
   the *entire* 2,534-item media library looking for a match (not found),
   the direct backend domain instead of the proxy, `_embed=1` -- none
   resolve the numeric IDs to a URL. Left out of SECTIONS until a working
   resolution path is found; the 71 posts / 813 files are real and
   confirmed to exist, just not currently downloadable through this API.
"""
import time

import requests

import classify
import storage
from crawler import enforce_filename_component_limit, enforce_folder_component_limit

SITE_TAG = "POWERMIN"
BASE = "https://www.powermin.gov.in"
API_BASE = f"{BASE}/cms/wp-json"
HEADERS = {
    "apikey": "4bW5t13453pa",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

# acf field name -> language label, used to disambiguate titles/filenames
# only when a post actually has more than one of these populated.
PDF_VARIANT_FIELDS = [("pdf", "EN"), ("pdf_hindi", "HI"), ("pdf_both", "EN-HI")]


def _get_with_retry(url: str, params: dict | None = None, retries: int = 6, backoff: float = 3.0):
    """powermin.gov.in's WAF intermittently returns a plain 403 "Access
    Denied" page for otherwise-valid requests (confirmed: the same exact
    request succeeds a few seconds later) -- a simple retry/backoff is
    enough to get through, no cookies or special headers needed."""
    last = None
    for _ in range(retries):
        resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
        if resp.status_code == 200:
            return resp
        last = resp
        time.sleep(backoff)
    return last


def _ddmmyyyy_to_iso(raw: str | None) -> str | None:
    if not raw:
        return None
    parts = raw.strip().split("/")
    if len(parts) != 3:
        return None
    d, m, y = parts
    try:
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    except ValueError:
        return None


def _record_api_document(conn, site_tag: str, min_year: int, folder_map: dict, doc_url: str,
                          found_on: str, title: str, original_filename: str,
                          document_date: str | None, category: str, subcategory: str) -> bool:
    """Same pattern as the other site-specific discovery modules
    (cspdcl_api_discovery.py etc.) -- original_filename/document_date come
    from the API's own data instead of being derived from doc_url, since
    the real URL here is just a content hash with no useful title in it
    (e.g. .../e13540c29dd4a2dc51cc4ec7d05d2996.pdf)."""
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


def discover_central_documents(conn, cfg) -> tuple[int, int]:
    """Paginates the entire central_documents collection (~1,827 posts,
    ~19 pages at per_page=100). Each post can yield up to 3 real files
    (English, Hindi, combined), each already resolved to a full object
    with a real `url` by WordPress itself -- no per-post extra request."""
    new_count = 0
    total_count = 0
    page = 1
    while True:
        resp = _get_with_retry(f"{API_BASE}/wp/v2/central_documents",
                                params={"per_page": 100, "page": page, "acf_format": "standard"})
        if resp is None or resp.status_code != 200:
            break
        items = resp.json()
        if not items:
            break

        for item in items:
            acf = item.get("acf") or {}
            title = str(acf.get("title") or item.get("title", {}).get("rendered") or "").strip()
            document_date = _ddmmyyyy_to_iso(acf.get("file_date"))
            found_on = f"{BASE}/documents/central/{item.get('slug', '')}"

            present = [(field, label) for field, label in PDF_VARIANT_FIELDS
                       if isinstance(acf.get(field), dict) and acf[field].get("url")]
            for field, label in present:
                pdf = acf[field]
                doc_url = pdf["url"]
                total_count += 1
                item_title = f"{title} ({label})" if len(present) > 1 else title
                original_filename = str(pdf.get("filename") or doc_url.rsplit("/", 1)[-1])

                if _record_api_document(conn, cfg.site_tag, cfg.min_year, cfg.folder_map, doc_url,
                                         found_on, item_title, original_filename, document_date,
                                         "CENTRAL_DOCUMENTS", "GENERAL"):
                    new_count += 1

        total_pages = int(resp.headers.get("X-WP-TotalPages", page))
        if page >= total_pages:
            break
        page += 1
    return new_count, total_count


def discover_all(conn, cfg) -> dict[str, tuple[int, int]]:
    """Runs every known, resolvable document source for this site."""
    return {"central_documents": discover_central_documents(conn, cfg)}


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
