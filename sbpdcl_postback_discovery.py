"""SBPDCL-specific document discovery via ASP.NET __doPostBack links.

SBPDCL's tender detail pages (e.g. frmTenderDetailsO.aspx) have zero plain
<a href> document links -- but have ones like:

    <a id="MainContent_lb/26/Tender Notice/scan0402.pdf"
       href="javascript:__doPostBack('ctl00$MainContent$lb/26/Tender Notice/scan0402.pdf','')">
       scan0402.pdf
    </a>

The control's own ID string literally encodes the file's identity --
unusual, but confirmed real and reproducible. Replicating the real
postback (POST back to the same page with that control ID as
__EVENTTARGET, carrying the page's own __VIEWSTATE/__EVENTVALIDATION)
returns the actual file directly (confirmed: Content-Disposition:
attachment, real %PDF bytes). See notes/ISSUES_LOG.md.

Must download immediately at discovery time, in the same page load that
produced the viewstate tokens -- ASP.NET viewstate is tied to that exact
render and can't be replayed later the way a normal URL can, so this
can't fit the usual crawl-now/download-later split. Called per visited
page from crawl_site() when cfg.postback_documents is set; a site without
that flag pays zero cost.
"""
import hashlib
import html as html_module
import re
from pathlib import Path

import classify
import storage

# The href attribute itself is HTML-entity-encoded in the page source
# (__doPostBack(&#39;...&#39;,&#39;&#39;)), not literal quotes -- confirmed
# real case: matching only literal ' found zero links on a page that
# clearly has them. html.unescape() is applied to the whole page before
# this regex runs (see extract_postback_document_links()).
POSTBACK_TARGET_RE = re.compile(r"__doPostBack\('([^']+)'\s*,\s*''\)")

_VIEWSTATE_FIELDS = ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION")


def extract_postback_document_links(html: str, extensions: set) -> list[str]:
    """Returns __EVENTTARGET control IDs whose own ID ends (after its last
    '/') with a recognized document extension -- the specific, unusual
    shape that makes this detection safe: an ordinary ASP.NET control ID
    never looks like a file path, so this never fires on a normal site."""
    targets = set(POSTBACK_TARGET_RE.findall(html_module.unescape(html)))
    return [t for t in targets if Path(t.split("/")[-1]).suffix.lower() in extensions]


def _extract_viewstate_fields(html: str) -> dict:
    fields = {}
    for name in _VIEWSTATE_FIELDS:
        m = re.search(rf'id="{name}" value="([^"]*)"', html)
        fields[name] = html_module.unescape(m.group(1)) if m else ""
    return fields


def fetch_postback_document(session, page_url: str, html: str, control_id: str, timeout: int = 30) -> bytes | None:
    """POSTs back to page_url with control_id as __EVENTTARGET, replicating
    the real click. Returns None (never raises) if the response doesn't
    look like a real attachment -- stale viewstate, wrong control, etc."""
    data = {"__EVENTTARGET": control_id, "__EVENTARGUMENT": "", **_extract_viewstate_fields(html)}
    try:
        resp = session.post(page_url, data=data, timeout=timeout)
    except Exception:
        return None
    if "attachment" not in resp.headers.get("Content-Disposition", "").lower() or not resp.content:
        return None
    return resp.content


def discover_and_download(conn, cfg, session, url: str, html: str, extensions: set) -> int:
    """Finds postback document links on one already-fetched page and
    downloads each immediately. Returns how many new documents were
    recorded (already-known ones, by this same pseudo-URL, are skipped)."""
    new_count = 0
    for control_id in extract_postback_document_links(html, extensions):
        filename_hint = control_id.split("/")[-1]
        pseudo_url = f"{url}#postback={control_id}"
        if storage.get_document(conn, pseudo_url) or storage.find_by_identity(conn, pseudo_url, cfg.site_tag):
            continue

        content = fetch_postback_document(session, url, html, control_id)
        if content is None:
            storage.log_breaker(conn, cfg.site_tag, pseudo_url, "n/a",
                                 "postback document fetch returned no attachment",
                                 "skipped", "still failed")
            continue

        category, subcategory, needs_verification = classify.classify_url(
            pseudo_url, filename_hint, "MISC", "GENERAL", cfg.keyword_rules, url)
        title = classify.build_document_title(filename_hint, filename_hint)
        document_date, date_basis_note = classify.extract_date(pseudo_url, filename_hint)
        language = classify.detect_language(pseudo_url, url)
        new_filename, target_folder_path, naming_scheme, verification_note = classify.resolve_filename_and_folder(
            cfg.site_tag, category, subcategory, title, language, document_date, filename_hint,
            pseudo_url, filename_hint, url, cfg.folder_map,
        )
        new_filename = storage.disambiguate_filename(conn, new_filename, pseudo_url, cfg.site_tag)

        from crawler import enforce_filename_component_limit, enforce_folder_component_limit, is_valid_file_content
        new_filename = enforce_filename_component_limit(new_filename)
        target_folder_path = enforce_folder_component_limit(target_folder_path)
        expected_ext = Path(new_filename).suffix
        if not is_valid_file_content(content, expected_ext):
            storage.log_breaker(conn, cfg.site_tag, pseudo_url, "n/a",
                                 f"postback response didn't look like a real {expected_ext} file",
                                 "skipped", "still failed")
            continue

        local_path = cfg.output_dir / target_folder_path / new_filename
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(content)
        sha256 = hashlib.sha256(content).hexdigest()

        if verification_note:
            date_basis_note = f"{date_basis_note}; {verification_note}"
        storage.insert_document(conn, {
            "doc_url": pseudo_url, "site_tag": cfg.site_tag, "found_on": url,
            "category": category, "subcategory": subcategory, "document_title": title,
            "original_filename": filename_hint, "file_type": classify.get_file_type(filename_hint),
            "language": language, "document_date": document_date, "date_basis_note": date_basis_note,
            "new_filename": new_filename, "target_folder_path": target_folder_path,
            "needs_verification": "No" if document_date else "Yes", "naming_scheme": naming_scheme,
        })
        storage.mark_downloaded(conn, pseudo_url, True, None, str(local_path), sha256)
        new_count += 1
    return new_count
