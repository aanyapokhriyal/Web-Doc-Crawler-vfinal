"""
crawler.py — crawl loop, fetch-tier escalation, and CLI for the multi-site
document crawler. Uses classify.py for category/date/language/filename
decisions and storage.py for all persistence and Excel export.

FETCH-TIER LADDER (cheapest first, escalate only on failure):
  Tier 1 — plain requests. No browser, fastest, works on fully
           server-rendered sites (e.g. wrldc per project testing).
  Tier 2 — stealth-headless Playwright: launch ONCE per site to bootstrap
           past a bot-check and capture cookies, then reuse those cookies
           in a plain requests.Session for every subsequent page (Sandeep's
           pattern). Still headless, so it can run unattended on a bare
           server. Escalates back to a per-page Playwright render if a
           specific page still comes back blocked/empty.
  Tier 3 — full headed (visible) Chromium. Last resort — needs an actual
           display attached to the machine (or Xvfb). Only powermin.gov.in
           has been confirmed to require this so far.

A site's fetch tier is detected FRESH every run, starting at tier 1, and is
NEVER written back to that site's config file — a site's tech stack can
change, and trusting a stale "this site = tier 1" assumption risks a
future run silently returning nothing. Within a single run, once a site's
first page reveals its required tier, the rest of that run's pages for
that site go straight there — no re-testing cheaper tiers per page.

Every escalation, robots.txt block, leaf-URL trigger, and suspected SPA
shell gets logged via storage.log_breaker() — live, per run, never
hand-curated.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import ssl
import time
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin, urlparse, parse_qs
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import classify
import storage

SCRIPT_DIR = Path(__file__).resolve().parent
SITES_DIR = SCRIPT_DIR / "sites"
MASTER_REPORTS_DIR = SCRIPT_DIR / "_MASTER_REPORTS"
WORKBOOKS_DIR = SCRIPT_DIR / "_WORKBOOKS"

FILE_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".rar", ".csv", ".txt", ".rtf", ".odt", ".ods", ".odp",
}
# Assets that are never worth crawling as "pages" — a plain requests fetch
# of one of these returns a non-HTML Content-Type, which the tier-1 fetch
# would otherwise misread as a failure requiring escalation (a live run
# against iexindia.com hit exactly this on /images/IEX_Logo_RGB_1.jpeg,
# wastefully escalating all the way to a headed-browser bootstrap just to
# fetch a logo). These are filtered out BEFORE queuing, not after.
# NOTE: .zip/.rar used to be here too, but a real CERC reference count
# showed genuine regulatory content (bundled multi-file submissions)
# served as .zip/.rar — moved to FILE_EXTENSIONS above instead.
SKIP_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".bmp",
    ".css", ".js", ".woff", ".woff2", ".ttf", ".eot",
    ".mp4", ".mp3", ".avi", ".mov", ".wmv", ".xml"
}
# Path substrings that suggest a URL is a file-serving handler rather than
# an ordinary page, even though it has a real (non-document) extension
# like .aspx/.php -- confirmed real case: SSNNL's download.aspx?couchdbdocid=
# serves real PDFs. Deliberately a short, specific list, not "any extension
# we don't recognize" -- that broader version was confirmed to also treat
# every ordinary .php/.aspx navigation page on a traditional government
# site as worth an individual HEAD request (41 on TGERC's homepage alone),
# which is wasted cost almost everywhere and meaningfully slows down any
# site with many such pages.
DOCUMENT_HANDLER_PATH_KEYWORDS = ("download", "getfile", "viewdoc", "attachment")
DEFAULT_MAX_DEPTH = 10
DEFAULT_MIN_YEAR = 1900
DEFAULT_MAX_TOTAL_PAGES = 10000
DEFAULT_MAX_PAGES_PER_LISTING = 200
# A response can be technically "successful" (200 OK, right content-type,
# enough raw bytes) while still being functionally useless -- confirmed on
# a live powermin.gov.in fetch: the raw HTML was barely more than a
# doctype/viewport tag (real navigation is added client-side by
# JavaScript, which a plain requests fetch never executes), yielding
# ZERO extractable links, yet it passed every existing check and the
# crawl ended immediately with 0 documents.
#
# Deliberately set to 1, not some stricter number -- an earlier version of
# this constant used 2, which immediately produced a real false positive:
# a legitimately sparse (but entirely valid) single-document test page
# was wrongly flagged as a JS-shell and escalated needlessly. The actual
# powermin evidence was a page with ZERO links, so requiring "at least
# one real link" both catches that exact failure and doesn't punish a
# genuinely minimal real page.
#
# Used ONLY for the initial site-probing fetch (see require_content in
# SiteFetcher.fetch) -- deliberately NOT applied to every page in a crawl,
# so a legitimately sparse real page deep in a site can't trigger a false
# escalation partway through.
MIN_MEANINGFUL_LINKS_FOR_SEED_PAGE = 1
LEAF_QUERY_PARAM_THRESHOLD = 2       # multisite's dashboard-explosion guard
THROTTLE_SECONDS = 1.0
REQUEST_TIMEOUT = 20
# Short, no-retry timeout for the speculative HTTP-vs-HTTPS probes in
# SiteFetcher (_try_better_http_seed / _try_http_downgrade) -- confirmed
# real case: a closed/filtered plain-HTTP port doesn't always refuse the
# connection outright, it can just hang, so this must fail fast rather
# than inheriting REQUEST_TIMEOUT (paid once per site regardless of
# whether that site ever needed the check).
HTTP_PROBE_TIMEOUT = 5
PAGE_TIMEOUT_MS = 45_000
# Automatic retry ceiling for a document download that times out at the
# normal 30s (see download_one()) -- confirmed real, recurring case:
# TNERC's server consistently needs longer than 30s for every single one
# of its documents (1088 for 1088 confirmed "Read timed out" failures).
EXTENDED_DOWNLOAD_TIMEOUT = 90
# A "page" fetch is expected to be an HTML document, normally KBs to a few
# MB -- confirmed real crash: downtoearth.org.in's /collection/top-news-waste
# page (a tag/archive listing with no pagination) grew large enough that
# just decoding it to text raised a bare MemoryError 136 minutes and 850+
# pages into a real crawl, which crashed the entire run since MemoryError
# isn't a requests.RequestException. Anything past this ceiling is treated
# as "not real page content" and skipped, the same as a non-HTML response.
MAX_PAGE_RESPONSE_BYTES = 25_000_000
# NTFS hard limit on a single path COMPONENT (a filename by itself), separate
# from and unaffected by the \\?\ long-path prefix used elsewhere in this
# file (that prefix only lifts the ~32K limit on the TOTAL path length).
# Confirmed real crash: BERC generates filenames from full document titles
# with no length cap, and 140 of them exceeded this per-component limit,
# failing every write with "WinError 123: The filename, directory name, or
# volume label syntax is incorrect."
MAX_FILENAME_COMPONENT_LENGTH = 255
JS_SETTLE_POLL_MS = 300              # adaptive-wait poll interval
JS_SETTLE_CEILING_MS = 6_000         # never wait longer than multisite did
DEFAULT_DOWNLOAD_CONCURRENCY = 4     # download phase only, never crawling
# Substrings seen in real Playwright exceptions when the browser/driver
# process itself has died (as opposed to an ordinary per-page navigation
# failure like a timeout or a live site returning an error) — confirmed
# against a real Chromium crash on downtoearth.org.in's old sitemap
# archive. Tier 3 has no further tier to escalate to, so once one of
# these is seen there, every remaining page for the rest of this site's
# run is short-circuited instead of repeating a guaranteed-failed
# Playwright call against a connection that's already gone.
DEAD_DRIVER_SIGNALS = ("Connection closed", "Browser has been closed",
                        "Target page, context or browser has been closed")
SPA_SHELL_MIN_PAGES = 5
SPA_SHELL_DOMINANT_RATIO = 0.6       # if >=60% of pages hash identical, suspect an SPA shell

USER_AGENT_PLAIN = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
USER_AGENT_REAL = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
window.chrome = { runtime: {} };
"""

PAGINATION_TEXT_RE = re.compile(r"^\s*(next|»|>|older|more)\s*$", re.IGNORECASE)
PAGINATION_URL_RE = re.compile(r"[?&](page|pg|start)=\d+", re.IGNORECASE)
_DOC_URL_PATTERN_CACHE = {}


# ---------------------------------------------------------------------------
# Site config
# ---------------------------------------------------------------------------

@dataclass
class SiteConfig:
    site_tag: str
    output_dir: Path
    allowed_domains: set
    seeds: list  # list of [url, category, subcategory]
    min_year: int = DEFAULT_MIN_YEAR
    max_depth: int = DEFAULT_MAX_DEPTH
    max_total_pages: int = DEFAULT_MAX_TOTAL_PAGES
    respect_robots: bool = False
    download_concurrency: int = DEFAULT_DOWNLOAD_CONCURRENCY
    extra_keyword_rules: list = field(default_factory=list)
    extra_folder_map: dict = field(default_factory=dict)
    use_fixed_js_wait: bool = False
    domain_blind_documents: bool = True
    allow_legacy_ssl_renegotiation: bool = False
    disable_ssl_verification: bool = False
    session_refresh: dict = field(default_factory=dict)
    postback_documents: bool = False
    excluded_domains: list = field(default_factory=list)

    @property
    def metadata_dir(self) -> Path:
        return self.output_dir / "_METADATA"

    @property
    def checkpoint_path(self) -> Path:
        return self.metadata_dir / "crawl_checkpoint.json"

    @property
    def keyword_rules(self) -> list:
        return classify.merge_keyword_rules(self.extra_keyword_rules)

    @property
    def folder_map(self) -> dict:
        return classify.merge_folder_map(self.extra_folder_map)


def resolve_config_path(raw: str) -> Path:
    p = Path(raw)
    if p.suffix == ".json" and p.exists():
        return p
    candidate = SITES_DIR / f"{raw}.json"
    if candidate.exists():
        return candidate
    if p.exists():
        return p
    raise SystemExit(f"Could not find a config at '{raw}' or '{candidate}'. Use --init-config to create one.")


def load_site_config(path: Path) -> SiteConfig:
    raw = json.loads(path.read_text())
    extra_rules = [(pattern, tuple(cat_sub)) for pattern, cat_sub in raw.get("extra_keyword_rules", [])]
    extra_folder_map = {tuple(k.split("|", 1)): v for k, v in raw.get("extra_folder_map", {}).items()}
    return SiteConfig(
        site_tag=raw["site_tag"].upper(),
        output_dir=Path(raw["output_dir"]).expanduser(),
        allowed_domains=set(raw["allowed_domains"]),
        seeds=[list(s) for s in raw["seeds"]],
        min_year=int(raw.get("min_year", DEFAULT_MIN_YEAR)),
        max_depth=int(raw.get("max_depth", DEFAULT_MAX_DEPTH)),
        max_total_pages=int(raw.get("max_total_pages", DEFAULT_MAX_TOTAL_PAGES)),
        respect_robots=bool(raw.get("respect_robots", False)),
        download_concurrency=int(raw.get("download_concurrency", DEFAULT_DOWNLOAD_CONCURRENCY)),
        extra_keyword_rules=extra_rules,
        extra_folder_map=extra_folder_map,
        use_fixed_js_wait=bool(raw.get("use_fixed_js_wait", False)),
        domain_blind_documents=bool(raw.get("domain_blind_documents", True)),
        allow_legacy_ssl_renegotiation=bool(raw.get("allow_legacy_ssl_renegotiation", False)),
        disable_ssl_verification=bool(raw.get("disable_ssl_verification", False)),
        session_refresh=dict(raw.get("session_refresh", {})),
        postback_documents=bool(raw.get("postback_documents", False)),
        excluded_domains=list(raw.get("excluded_domains", [])),
    )


def _normalize_base_url(base_url: str) -> str:
    """A URL with no scheme (e.g. 'www.niti.gov.in' instead of
    'https://www.niti.gov.in') fails identically at EVERY fetch tier —
    requests raises MissingSchema, Playwright raises a navigation error —
    so every tier looks like it failed for a real reason when the actual
    problem is just a missing 'https://'. Caught this from a real run
    where the failure trail (three tiers, all failing) made a one-word fix
    look like a much bigger problem than it was. Fixed here, once, at
    config-creation time, instead of leaving every fetch tier to rediscover
    the same invalid URL independently."""
    parsed = urlparse(base_url)
    if not parsed.scheme:
        fixed = f"https://{base_url}"
        print(f"  Note: --base-url had no http:// or https:// prefix — assuming {fixed}")
        base_url = fixed
        parsed = urlparse(base_url)
    # urlparse alone doesn't reject garbage like "https://not a url" — it
    # happily treats the whole remainder as a netloc. A netloc with
    # whitespace, or with no dot at all, isn't a real domain regardless of
    # what urlparse thinks.
    if not parsed.netloc or " " in parsed.netloc or "." not in parsed.netloc:
        raise SystemExit(f"'{base_url}' doesn't look like a valid URL (no real domain found). "
                          f"Expected something like 'https://example.gov.in'.")
    return base_url


def write_init_config(site_tag: str, base_url: str, output_dir: str, extra_seeds: list[str]) -> Path:
    SITES_DIR.mkdir(parents=True, exist_ok=True)
    base_url = _normalize_base_url(base_url)
    extra_seeds = [_normalize_base_url(s) for s in extra_seeds]
    netloc = urlparse(base_url).netloc
    domains = {netloc, netloc[4:] if netloc.startswith("www.") else f"www.{netloc}"}
    seeds = [[base_url.rstrip("/"), "MISC", "GENERAL"]] + [[u, "MISC", "GENERAL"] for u in extra_seeds]

    config = {
        "site_tag": site_tag.upper(),
        "output_dir": output_dir,
        "allowed_domains": sorted(domains),
        "seeds": seeds,
        "min_year": DEFAULT_MIN_YEAR,
        "max_depth": DEFAULT_MAX_DEPTH,
        "max_total_pages": DEFAULT_MAX_TOTAL_PAGES,
        "respect_robots": False,
        "download_concurrency": DEFAULT_DOWNLOAD_CONCURRENCY,
        "extra_keyword_rules": [],
        "extra_folder_map": {},
        "use_fixed_js_wait": False,
        "domain_blind_documents": True,
        "_notes": (
            "Auto-generated by --init-config. No fetch tier is stored here "
            "on purpose — it's re-detected fresh every run (see crawler.py "
            "module docstring). Add known listing pages to 'seeds' to speed "
            "up discovery; nav-menu and sitemap.xml scanning will also "
            "auto-add classified seeds on first crawl. robots.txt is "
            "checked and logged either way but does NOT block the crawl by "
            "default (real government-site robots.txt files are often "
            "overly broad and would block the exact public documents this "
            "tool exists to find) — set respect_robots to true, or pass "
            "--respect-robots, to actually enforce it for this site. "
            "use_fixed_js_wait: false uses a fast adaptive wait for "
            "JavaScript to finish rendering a page (exits early once the "
            "page stops changing) — set to true only for a site confirmed "
            "to need a full, unconditional 6-second wait on every single "
            "page (slower, but matches an older tool's proven-reliable "
            "behavior; confirmed necessary for powermin.gov.in specifically). "
            "domain_blind_documents: true (the default for new configs) "
            "means a document link is recorded regardless of which domain "
            "it's actually hosted on — real sites very commonly serve their "
            "actual files from a different subdomain than the page linking "
            "to them (a CDN, an asset host, a document-management-system "
            "subdomain: e.g. grid-india.in's pages link to files hosted on "
            "webcdn.grid-india.in). The domain a document happens to be "
            "hosted on says nothing about whether it's wanted — the page "
            "that linked to it already earned that by being in scope. Set "
            "to false only if this site is known to be at real risk of a "
            "malicious/compromised element (a hijacked ad, a poisoned "
            "iframe) pointing to an unrelated, untrusted domain with a "
            "document-like extension. Page-following also automatically "
            "follows a link into any subdomain of this site's own "
            "allowed_domains (e.g. report.erldc.in from erldc.in) "
            "regardless of this setting — only a domain that ISN'T even a "
            "subdomain of one already configured here needs "
            "domain_blind_documents=true to have its documents recorded, "
            "and page-following never extends to it at all."
        ),
    }
    out_path = SITES_DIR / f"{site_tag.lower()}.json"
    out_path.write_text(json.dumps(config, indent=2))
    return out_path


def batch_add_sites(csv_path: Path) -> tuple[list, list]:
    created, skipped = [], []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            site_tag = (row.get("site_tag") or "").strip()
            base_url = (row.get("base_url") or "").strip()
            if not site_tag or not base_url:
                continue
            existing = SITES_DIR / f"{site_tag.lower()}.json"
            if existing.exists():
                skipped.append(site_tag)
                continue
            output_dir = (row.get("output_dir") or "").strip() or str(SCRIPT_DIR / "downloads" / site_tag.upper())
            extra_seeds = [s.strip() for s in (row.get("extra_seeds") or "").split(";") if s.strip()]
            created.append((site_tag, write_init_config(site_tag, base_url, output_dir, extra_seeds)))
    return created, skipped


def list_all_configs() -> list[Path]:
    if not SITES_DIR.exists():
        return []
    return sorted(p for p in SITES_DIR.glob("*.json") if p.stem.upper() != "TEMPLATE")


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------

def load_robots(base_url: str, conn=None, site_tag: str | None = None):
    """Always attempts to load robots.txt, regardless of whether it will
    actually be enforced — SiteConfig.respect_robots controls whether a
    disallowed URL is skipped or just logged and fetched anyway, but
    either way we need the parsed rules to know which case applies.

    Note: a plain 404 (no robots.txt file at all -- the common case) never
    reaches the except below; Python's own RobotFileParser.read() already
    treats a 4xx HTTPError as "allow everything" internally without
    raising. Only a genuine connectivity failure (DNS, connection refused,
    timeout, SSL) gets here -- previously silent (not even printed), now
    logged so a site whose robots.txt is simply unreachable is
    distinguishable from one that was actually checked and found
    permissive."""
    parsed = urlparse(base_url)
    rp = robotparser.RobotFileParser()
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        rp.set_url(robots_url)
        rp.read()
        return rp
    except Exception as e:
        if conn is not None and site_tag is not None:
            storage.log_breaker(conn, site_tag, robots_url, "n/a",
                                 f"robots.txt fetch failed: {e}",
                                 "treated as allow-all", "n/a")
        return None  # no usable robots.txt -> treat as "allow everything"


def is_allowed_by_robots(rp, url: str) -> bool:
    if rp is None:
        return True
    try:
        return rp.can_fetch(USER_AGENT_PLAIN, url)
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Pure parsing helpers (fully offline-testable)
# ---------------------------------------------------------------------------

def normalize_url(url: str) -> str:
    """Strips the fragment -- except when it looks like a client-side
    router path (starts with '/', e.g. '#/pages/sm/tender/offices/...')
    rather than an ordinary same-page anchor (e.g. '#section2'). Angular/
    Vue-style hash-routed SPAs (confirmed real case: AVVNL, served from
    energy.rajasthan.gov.in/avvnl) put their entire site behind distinct
    '#/...' routes off a single real URL -- stripping the fragment made
    every one of those routes collapse into the same already-visited
    homepage URL, so the crawler found 153 real links on the rendered
    page but queued none of them, visited 1 page, and found 0 documents."""
    parsed = urlparse(url)
    if parsed.fragment.startswith("/"):
        return parsed.geturl()
    return parsed._replace(fragment="").geturl()


def is_document_link(url: str, extensions=FILE_EXTENSIONS) -> bool:
    return Path(urlparse(url).path).suffix.lower() in extensions


# Only extensions with a reliable, well-known magic-byte signature go here.
# Deliberately NOT exhaustive -- .doc/.xls/.ppt (older OLE compound-file
# format), .csv/.txt/.rtf have no signature reliable enough to risk a false
# rejection of an already-working download, so they're simply never
# checked (identical to today's behavior: always accepted). Confirmed
# real, live case this validates against: dgvcl.com's
# viewpdf.php?...filename=X.pdf "document" actually serves the site's own
# HTML shell (Content-Type: text/html) instead of the real PDF -- with no
# check at all, that HTML would be silently saved as X.pdf and marked a
# successful download.
_ZIP_BASED_EXTENSIONS = {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".zip"}
_ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


def is_valid_file_content(content: bytes, extension: str) -> bool:
    """True if content's actual bytes look like the format `extension`
    claims -- or if `extension` has no signature reliable enough to check
    (existing behavior, unchanged, for anything not covered below)."""
    extension = extension.lower()
    if extension == ".pdf":
        return b"%PDF" in content[:1024]
    if extension in _ZIP_BASED_EXTENSIONS:
        return content[:4] in _ZIP_SIGNATURES
    if extension == ".rar":
        return content[:4] == b"Rar!"
    return True


def should_skip_asset(url: str, skip_extensions=SKIP_EXTENSIONS) -> bool:
    """True for images/CSS/JS/fonts/media — never worth queuing as a page
    to crawl, and never a document to record either."""
    return Path(urlparse(url).path).suffix.lower() in skip_extensions


def is_same_base_domain(netloc: str, allowed_domains: list[str]) -> bool:
    """True if netloc IS one of allowed_domains, or is a subdomain of one --
    e.g. 'webcdn.grid-india.in' matches allowed_domains=['grid-india.in',
    'www.grid-india.in'] because it ends with '.grid-india.in'. Confirmed
    real, currently-active gap: auditing every site config found only 7 of
    65 had domain_blind_documents=true; the other 50 (many written before
    that flag's default flipped to true, which only affects brand-new
    configs) still silently drop every document hosted on exactly this
    kind of same-site file/CDN subdomain -- GRID-INDIA's webcdn.grid-india.in,
    ERCKERALA's dev.erckerala.org. Deliberately NOT full public-suffix-list
    logic (no new dependency) -- derives "same base domain" purely from
    each site's own already-curated allowed_domains, so an unrelated
    domain (a hijacked/poisoned link) still does NOT match here; that
    stays gated behind the separate, explicit domain_blind_documents
    opt-in below.

    Compares hostname only, ignoring any port -- confirmed real case: HVPN
    serves its documents from hvpn.org.in:8443 (same host, a document/
    e-office subsystem on a different port), and urlparse()'s netloc
    includes the port, so an un-stripped comparison would wrongly treat
    this as a completely unrelated domain."""
    netloc = netloc.lower().split(":")[0]
    for allowed in allowed_domains:
        core = allowed.lower().split(":")[0].removeprefix("www.")
        if netloc == core or netloc.endswith("." + core):
            return True
    return False


def enforce_filename_component_limit(filename: str, max_length: int = MAX_FILENAME_COMPONENT_LENGTH) -> str:
    """Truncates just the name part (keeping the extension) so the final
    filename component always fits within max_length -- see
    MAX_FILENAME_COMPONENT_LENGTH's docstring for why this is a separate
    concern from the existing total-path-length handling."""
    if len(filename) <= max_length:
        return filename
    name_part, dot, extension = filename.rpartition(".")
    if not dot:
        return filename[:max_length]
    keep = max_length - len(extension) - 1
    return f"{name_part[:keep]}.{extension}"


def enforce_folder_component_limit(folder_path: str, max_length: int = MAX_FILENAME_COMPONENT_LENGTH) -> str:
    """Same NTFS per-component limit as enforce_filename_component_limit(),
    applied to EACH segment of a folder path -- confirmed real case: BERC's
    generated target_folder_path embeds the full (300+ character) document
    title as one subfolder segment, which crashes mkdir with WinError 123
    before a download even reaches the filename itself."""
    segments = folder_path.replace("\\", "/").split("/")
    return "/".join(seg[:max_length] for seg in segments)


def is_leaf_url(url: str, threshold: int = LEAF_QUERY_PARAM_THRESHOLD) -> bool:
    """A page whose OWN query string has >= threshold parameters is treated
    as a dead end: files on it are still recorded, its links are not
    followed further. Stops combinatorial dashboard explosions (multisite's
    finding: a real powermin run took 5+ hours without this)."""
    return len(parse_qs(urlparse(url).query)) >= threshold


# Path fragments belonging to well-known utility endpoints that generate a
# combinatorial explosion of otherwise-meaningless URLs -- confirmed real,
# currently-active case: PED_SERVICES' electricity.py.gov.in (Drupal) has a
# "styleswitcher" accessibility theme-toggle on every real page, each one
# linking back to itself as
# /styleswitcher/switch/nexus/custom/<theme>?destination=<that page's own
# path>. LEAF_QUERY_PARAM_THRESHOLD (>= 2 distinct query param NAMES) can't
# catch this -- it only ever has ONE param ("destination") -- so a real
# crawl visited 5,200 pages, 65% of them this single non-content utility,
# and was still nowhere near finished. This module ships with Drupal core
# and is common across many .gov.in sites, so this is deliberately a path
# pattern, not a site-specific flag -- fixes PED_SERVICES/electricity.py.gov.in
# now and any other Drupal site hitting the same trap in the future, with
# no config needed.
#
# Second confirmed case, same shape: PGVCL's www.pgvcl.com/freport/complaint.php
# is a consumer complaint-tracking page -- every complaint ever filed gets its
# own URL via a single "bhjid" param with a different (effectively random)
# value each time. Same reason LEAF_QUERY_PARAM_THRESHOLD can't catch it (one
# param name, not two), same result: 74,667 of 74,689 queued URLs (99.97%)
# were this one path, 18+ hours and 2,620 pages visited found zero new
# documents, all of it spent here instead of on real content.
#
# Third confirmed case, different shape but same outcome: KSEB's
# kseb.in/downloads/<token> and kseb.in/sbudownloads/<token> look like
# per-document file links (an opaque encrypted token per URL, linked from
# nearly every page's nav), but directly tested 37 different FRESH tokens
# pulled live off the homepage -- every one of them just re-serves KSEB's
# own homepage/nav shell (confirmed by content, not just status code: same
# ~155 nav links every time), never an actual file. Cross-checked against
# KSEB's own already-discovered documents: 2,412 of 2,431 (99.2%) already
# come from the normal, extension-matched kseb.in/uploads/ path -- these
# two patterns are not hiding real content, they're dead ends on the site's
# own side. Queuing them as pages anyway grew one crawl's queue to 354,460
# entries and crashed it outright (see save_checkpoint()'s MemoryError fix,
# same incident) for zero net documents.
UTILITY_URL_PATH_PATTERNS = ("/styleswitcher/", "/freport/complaint.php", "/downloads/", "/sbudownloads/")


def is_utility_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(pattern in path for pattern in UTILITY_URL_PATH_PATTERNS)


def is_pagination_link(abs_url: str, link_text: str) -> bool:
    return bool(PAGINATION_TEXT_RE.match(link_text or "") or PAGINATION_URL_RE.search(abs_url))


def _is_skippable_href(href: str) -> bool:
    """True for hrefs that are never worth resolving at all: mail/phone/JS
    links, and a bare '#' (no fragment to even evaluate). A non-empty
    fragment ('#section2', '#/pages/...', '#annual-reports') is NOT
    decided here anymore -- whether it's a genuine same-page anchor or a
    distinct virtual route depends on the page's own content, which only
    _resolve_fragment_url() (below) has access to."""
    if not href or href.startswith(("mailto:", "tel:", "javascript:")):
        return True
    return href == "#"


def _resolve_fragment_url(soup: BeautifulSoup, base_url: str, href: str) -> str:
    """Resolves href to an absolute URL, deciding whether to keep or strip
    a '#fragment' based on whether THIS PAGE actually has a matching
    id=/name= anchor -- not just the fragment's shape.

    A fragment starting with '/' is always kept (delegated to
    normalize_url(), unchanged) -- confirmed real case: AVVNL is an
    Angular SPA whose entire site lives behind '#/...' routes off one
    real URL; stripping those collapsed every distinct page into the
    homepage.

    A fragment that does NOT start with '/' but ALSO matches no id=/name=
    anywhere on this page is kept too -- confirmed real, different case:
    TGSOUTHERNPOWER's nav links to '#annual-reports', '#quarterly-reports'
    etc., but no id="annual-reports" (or similar) exists anywhere in the
    page -- a JS click-handler must swap in that section's real content,
    so stripping the fragment would wrongly collapse these into the
    homepage the same way AVVNL's did. A fragment that DOES match a real
    id=/name= on this page is a genuine same-page anchor (someone linking
    to '#section2' where <div id="section2"> truly exists) and is
    stripped as before -- same URL as the page itself, correctly not a
    new page to queue."""
    resolved = urljoin(base_url, href)
    fragment = urlparse(resolved).fragment
    if fragment and not fragment.startswith("/"):
        target_exists = soup.find(id=fragment) is not None or soup.find("a", attrs={"name": fragment}) is not None
        if not target_exists:
            return resolved
    return normalize_url(resolved)


def extract_links(html: str, base_url: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if _is_skippable_href(href):
            continue
        try:
            abs_url = _resolve_fragment_url(soup, base_url, href)
        except ValueError:
            # Malformed href on the SOURCE page (bad markup) -- confirmed
            # real case: a citation like "[1] https://..." pasted directly
            # into an href, which urljoin reads as an invalid IPv6-bracketed
            # netloc and raises rather than just failing to parse. Skip
            # just this one link rather than crashing the entire crawl.
            continue
        links.append((abs_url, a.get_text(strip=True)))
    return links


def extract_frame_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """<iframe>/<frame> src attributes, treated as pages to crawl into --
    confirmed real case: BSPHCL's entire site is an old-school frameset
    (built with Microsoft FrontPage), where the real navigation and
    content live in separate pages loaded via iframe src ('Links.htm',
    'main.htm') -- extract_links() never sees these (it only scans
    <a href>), and extract_embedded_document_urls() never follows them
    further either (it only checks whether the iframe's OWN src is
    itself a document, not whether the page it loads has further links
    worth crawling). Without this, a frameset site's homepage looks like
    a dead end with zero real content, even though the real site is
    sitting one src attribute away. Deliberately only <iframe>/<frame>,
    not <embed>/<object> -- those two typically embed non-HTML content
    (a PDF, a video) directly rather than a navigable page."""
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for tag in soup.find_all(("iframe", "frame")):
        src = tag.get("src")
        if not src:
            continue
        try:
            abs_url = normalize_url(urljoin(base_url, src))
        except ValueError:
            # Same malformed-src protection as extract_links() -- see
            # that function's comment for the confirmed real case.
            continue
        links.append((abs_url, tag.get("title") or tag.get("name") or ""))
    return links


def extract_nav_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """For auto-seed classification: pull links specifically from <nav>
    elements and header-role containers, since those usually correspond to
    a site's own top-level section labels (Tenders, RTI, Annual Reports)."""
    soup = BeautifulSoup(html, "html.parser")
    containers = soup.find_all("nav") + soup.find_all(attrs={"role": "navigation"})
    links = []
    seen = set()
    for container in containers:
        for a in container.find_all("a", href=True):
            href = a["href"].strip()
            if _is_skippable_href(href):
                continue
            try:
                abs_url = _resolve_fragment_url(soup, base_url, href)
            except ValueError:
                # Same malformed-href protection as extract_links() -- see
                # that function's comment for the confirmed real case.
                continue
            if abs_url not in seen:
                seen.add(abs_url)
                links.append((abs_url, a.get_text(strip=True)))
    return links


def _document_url_pattern(extensions=FILE_EXTENSIONS):
    key = tuple(sorted(extensions))
    if key not in _DOC_URL_PATTERN_CACHE:
        pattern = r"https?://[^\s\"'<>\\]+?(?:" + "|".join(re.escape(e) for e in extensions) + r")"
        _DOC_URL_PATTERN_CACHE[key] = re.compile(pattern, re.IGNORECASE)
    return _DOC_URL_PATTERN_CACHE[key]


def extract_embedded_document_urls(html: str, extensions=FILE_EXTENSIONS, base_url: str | None = None) -> set[str]:
    """Some JS-framework sites (Next.js/Nuxt/etc.) ship document URLs
    inside a JSON blob in the raw HTML for client-side hydration, never as
    a real <a href> — regex-scans the full raw text independent of where
    it sits in the markup. (web-doc-crawler's technique; likely the key to
    iexindia-style sites per project testing.)

    Also extracts <iframe src>, <embed src>, and <object data> -- none of
    which extract_links() ever sees (it only looks at <a href>). The regex
    scan above already happens to catch an ABSOLUTE-URL iframe/embed/object
    (it doesn't care what tag a URL sits in), but confirmed real, live case
    (dgvcl.com's MGT-7 filing): a RELATIVE one -- e.g.
    <iframe src="usefulpdf/MGT_7_2024-2025.pdf"> -- is invisible to that
    regex (it requires a literal http(s):// prefix) and was being missed
    entirely. Only runs when base_url is given (needed to resolve a
    relative src via urljoin, same as extract_links() already does) --
    omitting it preserves the exact original regex-only behavior, so no
    existing caller is affected unless it opts in."""
    if not html:
        return set()
    found = set(_document_url_pattern(extensions).findall(html))
    if base_url:
        soup = BeautifulSoup(html, "html.parser")
        for tag_name, attr in (("iframe", "src"), ("embed", "src"), ("object", "data")):
            for tag in soup.find_all(tag_name):
                src = tag.get(attr)
                if not src:
                    continue
                try:
                    abs_url = normalize_url(urljoin(base_url, src))
                except ValueError:
                    # Same malformed-src protection as extract_links() --
                    # see that function's comment for the confirmed real case.
                    continue
                if is_document_link(abs_url, extensions):
                    found.add(abs_url)
    return found


def guess_extension_from_content_type(content_type: str) -> str | None:
    mapping = {
        "application/pdf": ".pdf",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.ms-excel": ".xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "text/csv": ".csv",
        "application/vnd.ms-powerpoint": ".ppt",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
        "application/zip": ".zip",
        "application/x-zip-compressed": ".zip",
        "application/vnd.rar": ".rar",
        "application/x-rar-compressed": ".rar",
        "text/plain": ".txt",
        "application/rtf": ".rtf",
        "text/rtf": ".rtf",
        "application/vnd.oasis.opendocument.text": ".odt",
        "application/vnd.oasis.opendocument.spreadsheet": ".ods",
        "application/vnd.oasis.opendocument.presentation": ".odp",
    }
    return mapping.get((content_type or "").split(";")[0].strip().lower())


def guess_extension_from_content_disposition(content_disposition: str) -> str | None:
    """Pulls the extension off a Content-Disposition header's filename
    parameter (e.g. 'attachment; filename="report.pdf"') -- a fallback for
    when Content-Type is missing or useless (confirmed real case: UPJVN
    sends Content-Type: "text" on every download, but a correct
    Content-Disposition filename every time). Handles both the plain
    filename=X and the quoted filename="X" forms; deliberately not
    handling RFC 5987's filename*=UTF-8''X form -- not seen on any site
    yet, and a missed extension here just means "treated as an ordinary
    page" (today's existing fail-safe), not a crash."""
    match = re.search(r'filename\s*=\s*"?([^";]+)"?', content_disposition or "", re.IGNORECASE)
    if not match:
        return None
    suffix = Path(match.group(1).strip()).suffix.lower()
    return suffix or None


def parse_sitemap_xml(xml_text: str) -> tuple[list[str], bool]:
    """Returns (urls, is_index). A sitemap index lists OTHER sitemap files
    (needs recursion by the caller); a regular sitemap lists real pages."""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return [], False
    tag = root.tag.lower()
    is_index = tag.endswith("sitemapindex")
    urls = [el.text.strip() for el in root.iter() if el.tag.lower().endswith("loc") and el.text]
    return urls, is_index


def looks_like_spa_shell(content_hashes: list[str], min_pages: int = SPA_SHELL_MIN_PAGES,
                          dominant_ratio: float = SPA_SHELL_DOMINANT_RATIO) -> bool:
    """True if a large share of visited pages returned byte-identical
    content — a sign the site only serves one JS shell and real navigation
    happens client-side (confirmed pattern on erldc in project testing).
    Link-following alone can't fix this; it's flagged so it's visible."""
    if len(content_hashes) < min_pages:
        return False
    most_common_count = Counter(content_hashes).most_common(1)[0][1]
    return (most_common_count / len(content_hashes)) >= dominant_ratio


def check_and_refresh_session(fetcher, cfg, url: str, result):
    """For a site whose server-rendered detail pages depend on ASP.NET
    session state set by an earlier listing-page visit, and silently fall
    back to serving that listing page's own content once the session has
    expired mid-crawl -- no error, no distinguishing status code, just the
    wrong page. Confirmed real case: SBPDCL's frmTenderDetailsO.aspx --
    a cold or stale-session fetch returns the generic "Active Tender List"
    page instead of the real tender (session default-timeout is ~20 min;
    a long crawl easily outlives it). Re-fetching the listing page in the
    SAME session re-establishes whatever server-side state the detail page
    needs, then the detail page is retried once.

    Opt-in per site via a `session_refresh` dict in that site's config
    (trigger_path_contains, fallback_signature, refresh_url) -- a site
    without it configured pays zero cost and sees zero behavior change,
    same as every other per-site opt-in flag in this file."""
    refresh_cfg = cfg.session_refresh
    if not refresh_cfg or result.html is None:
        return result
    if refresh_cfg.get("trigger_path_contains", "") not in url:
        return result
    if refresh_cfg.get("fallback_signature", "") not in result.html:
        return result
    refresh_url = refresh_cfg.get("refresh_url")
    if not refresh_url:
        return result
    fetcher.fetch(refresh_url)
    retried = fetcher.fetch(url)
    if retried.html is not None and refresh_cfg.get("fallback_signature", "") not in retried.html:
        return retried
    return result


# ---------------------------------------------------------------------------
# Checkpoint / resume (Parikshith's pattern)
# ---------------------------------------------------------------------------

def load_checkpoint(path: Path) -> tuple[set, list]:
    if not path.exists():
        return set(), []
    data = json.loads(path.read_text())
    return set(data.get("visited", [])), data.get("queue", [])


def save_checkpoint(path: Path, visited: set, queue_snapshot: list) -> None:
    """Streams JSON straight to the file instead of building the whole
    thing as one string first (json.dumps() + write_text()) -- confirmed
    real crash on KSEB: its queue grew to 354,460 items (a flood of
    per-document signed/session-bound download links being queued as
    ordinary pages -- see notes/ISSUES_LOG.md), and materializing that
    many objects into one giant JSON string on top of the Python objects
    already in memory raised a bare MemoryError, killing the crawl.
    json.dump() writes incrementally and never holds a second full copy
    of the data as one string, cutting peak memory substantially for a
    checkpoint this size -- zero behavior or cost change for every
    normal-sized site."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump({"visited": list(visited), "queue": queue_snapshot}, f)


# ---------------------------------------------------------------------------
# Fetch tiers
# ---------------------------------------------------------------------------

class _LegacySSLAdapter(HTTPAdapter):
    """For a site whose document-download backend only supports old-style
    TLS renegotiation -- modern OpenSSL (3.x) refuses that by default and
    raises 'UNSAFE_LEGACY_RENEGOTIATION_DISABLED', confirmed on NRLDC's
    download-file endpoint even though the site's own pages load fine.
    Deliberately opt-in per site (allow_legacy_ssl_renegotiation in that
    site's config) rather than a global relaxation, since this weakens a
    real security protection and should only apply to a confirmed site.

    disable_verification additionally skips certificate-chain validation
    entirely (for the separate "unable to get local issuer certificate"
    failure mode, confirmed on AERC/CSERC/MSERC/GRID-INDIA). This can't be
    done via plain requests.Session(verify=False) once a custom ssl_context
    is in play -- confirmed real error: supplying an explicit SSLContext
    means requests/urllib3 no longer touch its verification settings for
    you, and Python's ssl module itself raises "Cannot set verify_mode to
    CERT_NONE when check_hostname is enabled" unless check_hostname is
    turned off FIRST, in this exact order."""

    def __init__(self, *args, disable_verification: bool = False, **kwargs):
        self._disable_verification = disable_verification
        super().__init__(*args, **kwargs)

    def _build_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
        if self._disable_verification:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._build_context()
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = self._build_context()
        return super().proxy_manager_for(*args, **kwargs)


def build_retry_session(user_agent: str = USER_AGENT_PLAIN, allow_legacy_ssl: bool = False,
                         disable_ssl_verification: bool = False) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent})
    retry = Retry(total=4, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504],
                   allowed_methods=["GET", "HEAD"])
    adapter = HTTPAdapter(max_retries=retry)
    if allow_legacy_ssl:
        https_adapter = _LegacySSLAdapter(max_retries=retry, disable_verification=disable_ssl_verification)
    else:
        https_adapter = adapter
    if disable_ssl_verification:
        # Covers the plain-HTTPAdapter path too (no custom ssl_context
        # involved there, so this alone is sufficient in that case).
        session.verify = False
    session.mount("http://", adapter)
    session.mount("https://", https_adapter)
    return session


@dataclass
class FetchResult:
    html: str | None
    final_url: str
    status_code: int | None
    tier_used: int
    error: str | None = None


class SiteFetcher:
    """Manages the 3-tier escalation ladder for ONE site's crawl. Tier
    functions are injectable so the escalation state machine itself can be
    proven with fakes, independent of whether a real browser/network is
    available (see test_crawler_unit.py) — production use relies on the
    real implementations wired in by default.
    """

    def __init__(self, site_tag: str, conn, min_content_length: int = 300,
                 tier1_fetch=None, tier2_bootstrap=None, tier2_fetch=None,
                 tier3_bootstrap=None, tier3_fetch=None, use_fixed_js_wait: bool = False,
                 allow_legacy_ssl_renegotiation: bool = False):
        self.site_tag = site_tag
        self.conn = conn
        self.min_content_length = min_content_length
        self.active_tier = 1
        self.use_fixed_js_wait = use_fixed_js_wait
        self._tier1_fetch = tier1_fetch or self._real_tier1_fetch
        self._tier2_bootstrap = tier2_bootstrap or self._real_tier2_bootstrap
        self._tier2_fetch = tier2_fetch or self._real_tier2_fetch
        self._tier3_bootstrap = tier3_bootstrap or self._real_tier3_bootstrap
        self._tier3_fetch = tier3_fetch or self._real_tier3_fetch
        self._session = build_retry_session(allow_legacy_ssl=allow_legacy_ssl_renegotiation)
        # Automatic, config-free last-resort session for the crawl/discovery
        # phase -- mirrors the fallback session download_pending_documents()
        # already builds for the download phase, but that one is a
        # completely separate object never used here. Confirmed real,
        # currently-lost case: NRLDC's extensionless document links (e.g.
        # nrldc.in/download-file?any=...pdf) rely on sniff_content_type()'s
        # HEAD request to get classified as documents at all; that HEAD
        # request was going out on a plain session with no legacy-SSL
        # support and always failing with SSLError, so the link was never
        # recognized as a document, got queued as an ordinary page instead,
        # and a real browser then tried to "visit" it and crashed
        # ("Page.goto: Download is starting") without ever recording the
        # document. Only tried AFTER the normal session's request already
        # failed with an SSLError -- a site with no SSL problems never
        # touches this, zero extra cost.
        self._fallback_session = build_retry_session(allow_legacy_ssl=True, disable_ssl_verification=True)
        # Deliberately a PLAIN requests.Session with no Retry adapter mounted
        # (unlike self._session/self._fallback_session, both built via
        # build_retry_session() with total=4 retries + backoff) -- used only
        # for the speculative HTTP-vs-HTTPS probes below. Confirmed real,
        # serious problem: grid-india.in's plain port 80 doesn't refuse the
        # connection, it just hangs -- with the normal retry-heavy session,
        # a short per-attempt timeout still gets multiplied by 4 retries
        # plus backoff into 20-30+ seconds added to EVERY site's crawl, not
        # just ones that actually need the HTTP fallback. This session
        # fails fast, once, with a short timeout (see HTTP_PROBE_TIMEOUT),
        # so a closed/filtered port 80 costs a few seconds, not tens of
        # seconds, on a site that never needed this check to begin with.
        self._quick_probe_session = requests.Session()
        self._tier2_ready = False
        self._tier3_ready = False
        self._tier3_dead = False  # confirmed browser/driver crash, not just a page failure
        self._playwright_handles = {}  # tier -> (playwright, browser, context, page)

    # -- real implementations (need network / Playwright; not testable here) --

    def _real_tier1_fetch(self, url: str, timeout: int = REQUEST_TIMEOUT, session=None) -> FetchResult:
        session = session or self._session
        try:
            # stream=True + a hard byte cap so one pathologically large page
            # (no pagination on a tag/archive listing, a runaway dynamic
            # page, etc.) can never balloon memory the way a plain
            # session.get() + resp.text did in the confirmed real crash —
            # see MAX_PAGE_RESPONSE_BYTES above.
            try:
                resp = session.get(url, timeout=timeout, stream=True)
            except requests.exceptions.SSLError:
                # Automatic, config-free fallback -- same fallback session
                # already built for downloads and sniff_content_type(), now
                # also covering the main page fetch itself. Confirmed real
                # gap: NERC's certificate hostname mismatch failed here
                # with no retry at all, cascading into a total crawl
                # failure since Playwright's tiers can't get past a bad
                # cert either (see ignore_https_errors on both browser
                # contexts below). Only tried after the normal,
                # fully-verified request already failed -- a site with no
                # SSL problems never touches this, zero extra cost.
                resp = self._fallback_session.get(url, timeout=timeout, stream=True)
            content_length = resp.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_PAGE_RESPONSE_BYTES:
                resp.close()
                return FetchResult(None, url, resp.status_code, 1,
                                    f"response too large ({int(content_length):,} bytes), skipped")
            raw_chunks = []
            total_bytes = 0
            for chunk in resp.iter_content(chunk_size=65536):
                total_bytes += len(chunk)
                if total_bytes > MAX_PAGE_RESPONSE_BYTES:
                    resp.close()
                    return FetchResult(None, url, resp.status_code, 1,
                                        f"response exceeded {MAX_PAGE_RESPONSE_BYTES:,} bytes "
                                        f"while streaming, skipped")
                raw_chunks.append(chunk)
            raw = b"".join(raw_chunks)
        except (requests.RequestException, MemoryError) as e:
            return FetchResult(None, url, None, 1, str(e))
        if resp.status_code in (403, 429, 503):
            return FetchResult(None, resp.url, resp.status_code, 1, f"HTTP {resp.status_code}")
        if "text/html" not in resp.headers.get("Content-Type", ""):
            return FetchResult(None, resp.url, resp.status_code, 1, "non-HTML response")
        try:
            text = raw.decode(resp.encoding or "utf-8", errors="replace")
        except MemoryError as e:
            return FetchResult(None, resp.url, resp.status_code, 1, str(e))
        if len(text) < self.min_content_length:
            return FetchResult(None, resp.url, resp.status_code, 1, "suspiciously short content")
        return FetchResult(text, resp.url, resp.status_code, 1, None)

    def _real_tier2_bootstrap(self, url: str):
        from playwright.sync_api import sync_playwright
        try:
            playwright = sync_playwright().start()
            browser = playwright.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            context = browser.new_context(user_agent=USER_AGENT_REAL, viewport={"width": 1920, "height": 1080},
                                           ignore_https_errors=True)
            context.add_init_script(STEALTH_INIT_SCRIPT)
            page = context.new_page()
        except Exception as e:
            # Launch itself failed, before there's anywhere to register a
            # handle for close()/_close_tier_handle() to find later — clean
            # up whatever got created here so the Node.js driver process
            # (and any partially-launched Chromium) doesn't leak.
            for obj in (locals().get("browser"), locals().get("playwright")):
                if obj is not None:
                    try:
                        obj.close() if hasattr(obj, "close") else obj.stop()
                    except Exception:
                        pass
            return FetchResult(None, url, None, 2, str(e))
        # Register the handle BEFORE the risky navigate call, so close()/
        # _close_tier_handle() can still clean up this browser even if
        # goto() itself fails (e.g. the URL triggers a file download
        # instead of a page load — Playwright raises for that, it doesn't
        # return a normal response; a bare .zip/.exe/unexpected-filetype
        # URL slipping past should_skip_asset is exactly this case).
        self._playwright_handles[2] = (playwright, browser, context, page)
        try:
            response = page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            self._adaptive_settle(page)
            html = page.content()
        except Exception as e:
            return FetchResult(None, url, None, 2, str(e))
        for c in context.cookies():
            self._session.cookies.set(c["name"], c["value"], domain=c.get("domain"))
        return FetchResult(html, page.url, response.status if response else None, 2, None)

    def _real_tier2_fetch(self, url: str) -> FetchResult:
        # Cookies from bootstrap are already in self._session — try that first,
        # but only trust it if the page has real links. A plain (non-JS)
        # fetch of a client-side-routed SPA page always succeeds -- status
        # 200, real HTML, same bytes every time -- because the server never
        # even sees the URL's '#/...' fragment; it has no way to return
        # different content per route. Confirmed real case: AVVNL
        # (energy.rajasthan.gov.in/avvnl, an Angular hash-router) returned
        # this exact same content-free loading shell for every one of its
        # ~150 distinct routes, each accepted here as a "successful" tier-2
        # fetch and tagged tier_used=2 -- which is exactly what hides this
        # from crawl_site()'s "tier 1 result with zero links -> verify with
        # tier 2" safety net below (it only ever checks tier_used == 1, so a
        # shortcut result wrongly labeled tier 2 slips straight past it).
        # Checking for a real link here, in the one place that actually
        # knows this was a plain-fetch shortcut rather than a genuine
        # render, closes that gap at its source.
        result = self._real_tier1_fetch(url)
        if result.html is not None and extract_links(result.html, result.final_url):
            return FetchResult(result.html, result.final_url, result.status_code, 2, None)
        # Escalate this one page to the already-open stealth browser.
        _, _, context, page = self._playwright_handles[2]
        try:
            response = page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            self._adaptive_settle(page)
            html = page.content()
            for c in context.cookies():
                self._session.cookies.set(c["name"], c["value"], domain=c.get("domain"))
            return FetchResult(html, page.url, response.status if response else None, 2, None)
        except Exception as e:
            return FetchResult(None, url, None, 2, str(e))

    def _real_tier3_bootstrap(self, url: str):
        from playwright.sync_api import sync_playwright
        try:
            playwright = sync_playwright().start()
            browser = playwright.chromium.launch(headless=False)
            context = browser.new_context(user_agent=USER_AGENT_REAL, ignore_https_errors=True)
            page = context.new_page()
        except Exception as e:
            # Same launch-failure cleanup as tier 2's bootstrap — no handle
            # exists yet for close() to find, so clean up here directly.
            for obj in (locals().get("browser"), locals().get("playwright")):
                if obj is not None:
                    try:
                        obj.close() if hasattr(obj, "close") else obj.stop()
                    except Exception:
                        pass
            return FetchResult(None, url, None, 3, str(e))
        self._playwright_handles[3] = (playwright, browser, context, page)  # register before the risky goto
        try:
            response = page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            self._adaptive_settle(page)
            html = page.content()
        except Exception as e:
            if any(sig in str(e) for sig in DEAD_DRIVER_SIGNALS):
                self._tier3_dead = True
            return FetchResult(None, url, None, 3, str(e))
        return FetchResult(html, page.url, response.status if response else None, 3, None)

    def _real_tier3_fetch(self, url: str) -> FetchResult:
        if self._tier3_dead:
            # Tier 3 is the end of the escalation ladder — once its browser
            # or driver process is confirmed gone (not just one bad page),
            # there's nowhere further to escalate to, so stop paying the
            # cost of repeating a guaranteed-failed Playwright call for
            # every remaining URL in this site's queue.
            return FetchResult(None, url, None, 3, "tier-3 browser/driver already confirmed dead this run")
        _, _, _, page = self._playwright_handles[3]
        try:
            response = page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            self._adaptive_settle(page)
            return FetchResult(page.content(), page.url, response.status if response else None, 3, None)
        except Exception as e:
            if any(sig in str(e) for sig in DEAD_DRIVER_SIGNALS):
                self._tier3_dead = True
            return FetchResult(None, url, None, 3, str(e))

    def _adaptive_settle(self, page, ceiling_ms: int = JS_SETTLE_CEILING_MS, poll_ms: int = JS_SETTLE_POLL_MS):
        """Replaces multisite's fixed 6-second wait: poll for the DOM to
        stop growing, exit early on simple pages, never wait past the
        ceiling on complex ones.

        Sites where this early-exit genuinely loses real content (a page
        that renders a shell, pauses briefly, then injects real content a
        moment later via a delayed API call) can opt into
        use_fixed_js_wait in their own JSON config — this then falls back
        to exactly multisite's original approach: an unconditional,
        no-early-exit wait of the full ceiling on every single page.
        Confirmed directly from multisite's own source
        (EXTRA_JS_SETTLE_MS = 6000, always, no polling) as the proven
        approach for powermin.gov.in specifically. Every other site keeps
        the faster adaptive behavior below by default, unaffected."""
        if self.use_fixed_js_wait:
            page.wait_for_timeout(ceiling_ms)
            return
        waited = 0
        try:
            last_length = len(page.content())
        except Exception:
            return
        while waited < ceiling_ms:
            page.wait_for_timeout(poll_ms)
            waited += poll_ms
            try:
                current_length = len(page.content())
            except Exception:
                return
            if current_length == last_length:
                return
            last_length = current_length

    # -- escalation state machine (this part IS fully unit-testable via injected fakes) --

    def sniff_content_type(self, url: str, timeout: int = 10) -> tuple[str, str] | None:
        """HEAD-requests a URL to check its real Content-Type (and
        Content-Disposition) without downloading the body — for a link
        with no file extension at all that might still be a real
        document. Confirmed real need from GERC: /viewdocument/<opaque-token>
        URLs serve real files but have no recognizable extension, so
        is_document_link() alone can never catch them — they were being
        queued as ordinary pages, and a real browser trying to "visit" one
        instead started a file download, crashing that page with
        "Download is starting" and never recording the document.

        Also returns Content-Disposition -- confirmed real, separate gap
        from UPJVN: its server sends Content-Type: "text" (not a real
        MIME type, so guess_extension_from_content_type() can't map it)
        on every download, but ALSO sends a perfectly good
        Content-Disposition: attachment; filename="X.pdf" every time.
        Relying on Content-Type alone meant every one of these documents
        was wrongly treated as an ordinary page, hitting the exact same
        "Download is starting" crash as the GERC case above despite the
        HEAD request itself succeeding.

        Returns None only when the HEAD request itself fails outright
        (network error/timeout) -- "treat this as an ordinary page,"
        exactly matching today's existing behavior. A site with no
        extension-less links at all sees zero behavior change and pays
        zero extra cost."""
        try:
            resp = self._session.head(url, timeout=timeout, allow_redirects=True)
            return resp.headers.get("Content-Type", ""), resp.headers.get("Content-Disposition", "")
        except requests.exceptions.SSLError:
            try:
                resp = self._fallback_session.head(url, timeout=timeout, allow_redirects=True)
                return resp.headers.get("Content-Type", ""), resp.headers.get("Content-Disposition", "")
            except Exception:
                return None
        except Exception:
            return None

    def _evaluate_fetch(self, result: FetchResult, url: str, require_content: bool) -> tuple[bool, str]:
        """Returns (is_successful, failure_reason). When require_content is
        False (the default, used for every normal page in a crawl), this
        reduces exactly to the original "did we get any html at all" check
        — no behavior change there. When True (used only for the initial
        site-probing fetch), a page must ALSO contain at least
        MIN_MEANINGFUL_LINKS_FOR_SEED_PAGE real links to count as
        successful — see the constant's docstring for why.

        Counts extract_frame_links() alongside extract_links() -- confirmed
        real case: BSPHCL's homepage is an old-style frameset with zero
        <a href> links at all (its real nav lives inside an iframe src),
        so every tier's seed probe failed this check and the site was
        wrongly declared completely unreachable before extract_frame_links()
        even got a chance to find the real content one level in."""
        if result.html is None:
            return False, result.error or "no content"
        if require_content:
            link_count = len(extract_links(result.html, url)) + len(extract_frame_links(result.html, url))
            if link_count < MIN_MEANINGFUL_LINKS_FOR_SEED_PAGE:
                return False, (f"page loaded ({len(result.html)} bytes) but contained only "
                                f"{link_count} link(s) — likely a JavaScript-rendered shell "
                                f"a plain fetch can't see through")
        return True, ""

    def fetch(self, url: str, require_content: bool = False) -> FetchResult:
        if self.active_tier == 1:
            result = self._tier1_fetch(url)
            ok, reason = self._evaluate_fetch(result, url, require_content)
            if ok:
                if require_content:
                    # Confirmed real cases in BOTH directions: NERC's HTTPS
                    # vhost is misconfigured and returns a broken
                    # hosting-error page with just barely enough links (1)
                    # to pass the deliberately lenient
                    # MIN_MEANINGFUL_LINKS_FOR_SEED_PAGE check above -- so it
                    # never fails, never escalates, and never reaches the
                    # last-resort protocol-swap at the bottom of this
                    # method. TERC's server times out on plain HTTP
                    # entirely while HTTPS works fine -- the opposite
                    # direction. This is a SEPARATE, narrower check that
                    # only ever runs once per site (require_content is only
                    # True for the initial seed probe): if the other
                    # protocol's version of the exact same URL has MORE
                    # real links, it's almost certainly the real site while
                    # the configured one is broken, so prefer it. A
                    # genuinely fine seed (the overwhelming majority) just
                    # costs one extra cheap tier-1-only fetch and keeps its
                    # own result unchanged.
                    better = self._try_better_http_seed(url, result)
                    if better is not None:
                        return better
                return result
            self._log_escalation(url, 1, reason, "stealth-headless")
            self.active_tier = 2

        if self.active_tier == 2:
            if not self._tier2_ready:
                boot = self._tier2_bootstrap(url)
                self._tier2_ready = True
                ok, reason = self._evaluate_fetch(boot, url, require_content)
                if ok:
                    return boot
            else:
                result = self._tier2_fetch(url)
                ok, reason = self._evaluate_fetch(result, url, require_content)
                if ok:
                    return result
            self._log_escalation(url, 2, "blocked or empty after stealth-headless", "headed")
            # CRITICAL: tier 2's Playwright instance must be fully stopped
            # before tier 3 starts its own — two live sync_playwright()
            # instances in the same thread is unsupported and is exactly
            # what produced "Playwright Sync API inside the asyncio loop"
            # on a real run against iexindia.com. Only one tier's browser
            # is ever needed alive at a time; once escalated past tier 2,
            # its resources are no longer needed for the rest of this run.
            self._close_tier_handle(2)
            self.active_tier = 3

        if not self._tier3_ready:
            boot = self._tier3_bootstrap(url)
            self._tier3_ready = True
            ok, reason = self._evaluate_fetch(boot, url, require_content)
            if ok:
                storage.log_breaker(self.conn, self.site_tag, url, "headed", "n/a", "n/a", "succeeded (final tier)")
                return boot
            downgraded = self._try_http_downgrade(url, require_content)
            if downgraded is not None:
                return downgraded
            storage.log_breaker(self.conn, self.site_tag, url, "headed", reason,
                                 "no further tier available", "still failed")
            return FetchResult(None, boot.final_url, boot.status_code, 3, reason)

        result = self._tier3_fetch(url)
        ok, reason = self._evaluate_fetch(result, url, require_content)
        if not ok:
            downgraded = self._try_http_downgrade(url, require_content)
            if downgraded is not None:
                return downgraded
            storage.log_breaker(self.conn, self.site_tag, url, "headed", reason,
                                 "no further tier available", "still failed")
            return FetchResult(None, result.final_url, result.status_code, 3, reason)
        return result

    @staticmethod
    def _swap_protocol(url: str) -> str | None:
        """Returns the same URL with its scheme flipped (https<->http), or
        None if it's neither. Both directions are real, confirmed cases --
        NERC needs https->http (a broken HTTPS vhost serving the wrong
        content), TERC needs the opposite, http->https (the server has
        stopped accepting plain-HTTP connections at all, timing out on
        port 80, while HTTPS on 443 works perfectly). There's no way to
        know in advance which direction a future site will need, so both
        checks below try whichever protocol ISN'T the one that just
        failed."""
        if url.startswith("https://"):
            return "http://" + url[len("https://"):]
        if url.startswith("http://"):
            return "https://" + url[len("http://"):]
        return None

    def _try_http_downgrade(self, url: str, require_content: bool) -> FetchResult | None:
        """Last-resort recovery, tried only once every tier (1, 2, AND 3) has
        already failed for this URL over its configured protocol. Confirmed
        real, currently active cases in both directions (see
        _swap_protocol()'s docstring): NERC's HTTPS virtual host is
        misconfigured on shared hosting and serves a completely different,
        broken page (a CloudAccess.net hosting-error stub) than plain HTTP
        does for the exact same URL -- no exception is ever raised (it's a
        normal 200 response, just wrong content), so this can't be caught
        the way the SSL-certificate fallback catches a raised SSLError.
        TERC's server times out connecting on port 80 entirely (confirmed
        live) while HTTPS works fine -- the opposite direction, same
        underlying idea. Deliberately placed only at the two
        already-exhausted-every-tier exit points, not as an eager first
        choice: a working site never reaches this code at all (zero cost,
        zero risk of ever weakening a site that already works), and a
        genuinely dead site (nothing at either protocol) still correctly
        fails afterward since this goes through the exact same
        _evaluate_fetch() check as every other tier -- a swap that returns
        equally useless content is rejected, not blindly trusted just
        because it responded. Config-free: works for every existing and
        future site with no per-site flag to set or forget."""
        swapped_url = self._swap_protocol(url)
        if swapped_url is None:
            return None
        result = self._real_tier1_fetch(swapped_url, timeout=HTTP_PROBE_TIMEOUT, session=self._quick_probe_session)
        ok, _ = self._evaluate_fetch(result, swapped_url, require_content)
        if not ok:
            return None
        storage.log_breaker(self.conn, self.site_tag, url, "n/a",
                             "original protocol unusable at every tier",
                             f"recovered via {swapped_url}", "succeeded (final tier)")
        return result

    def _try_better_http_seed(self, seed_url: str, seed_result: FetchResult) -> FetchResult | None:
        """Narrower companion to _try_http_downgrade() above, for the one
        case that check can never reach: a seed that returns REAL
        (non-empty, correctly-typed) content and so never fails
        _evaluate_fetch() or escalates tiers at all, but that content is
        wrong -- confirmed real case: NERC's broken HTTPS vhost returns a
        hosting-error page with exactly 1 link, just barely enough to pass
        MIN_MEANINGFUL_LINKS_FOR_SEED_PAGE's deliberately lenient threshold
        (see its own docstring for why that threshold can't simply be
        raised). Only ever called once per site (require_content is True
        only for the initial seed probe), and uses the same short-timeout,
        no-retry quick-probe session as _try_http_downgrade() -- confirmed
        real, serious problem: grid-india.in's plain port 80 doesn't refuse
        the connection, it just hangs, so this must fail fast rather than
        adding tens of seconds to every single site's crawl regardless of
        whether it ever needed this check. Compares real link counts
        rather than trying to detect "this looks like an error page" -- a
        fragile, provider-specific heuristic -- so it only ever swaps in
        the other protocol's version when it is DEMONSTRABLY richer in
        real content, never on a guess."""
        swapped_url = self._swap_protocol(seed_url)
        if swapped_url is None:
            return None
        swapped_result = self._real_tier1_fetch(swapped_url, timeout=HTTP_PROBE_TIMEOUT, session=self._quick_probe_session)
        if not swapped_result.html:
            return None
        seed_links = len(extract_links(seed_result.html, seed_result.final_url))
        swapped_links = len(extract_links(swapped_result.html, swapped_result.final_url))
        if swapped_links <= seed_links:
            return None
        storage.log_breaker(self.conn, self.site_tag, seed_url, "n/a",
                             f"seed has only {seed_links} link(s), {swapped_url} has {swapped_links}",
                             f"switched to {swapped_url}", "succeeded (final tier)")
        return swapped_result

    def verify_empty_page_with_tier2(self, url: str) -> FetchResult:
        """Independent, per-page safety net -- deliberately NOT part of the
        sticky active_tier state machine above. A page that returns
        non-empty, correctly-typed HTML at tier 1 but contains zero real
        links is often a client-side-rendered app shell (confirmed real
        case: GRID-INDIA's "daily PSP report" page, a pure React SPA route
        whose real content -- and its actual document links -- only exist
        after JS runs and calls its own backend API) rather than a
        genuinely tier-1-incompatible site. Permanently escalating the
        WHOLE site's active_tier for the rest of the run, the way the
        state machine above does, would be the wrong fix here: it would
        also wrongly slow down every other page even though most of them
        are fine at tier 1 (confirmed: GRID-INDIA's other 1219 documents
        ARE found via plain tier 1), and would incorrectly punish a
        legitimately link-sparse real page elsewhere on the same site --
        exactly the false-escalation risk _evaluate_fetch()'s
        require_content check is deliberately scoped away from for
        ordinary pages. So this re-fetches just THIS one URL via tier 2
        and lets the caller compare results, leaving active_tier and every
        other page's tier choice completely untouched. Deliberately capped
        at tier 2, not tier 3 -- tier 3 launches a real, visible browser
        window and is a rare, deliberate last resort, not something to
        trigger automatically and silently on every empty-looking page
        across every site.

        Checks for the handle itself, not just _tier2_ready -- confirmed
        real crash on JBVNL: once a site fully escalates past tier 2 to
        tier 3, _close_tier_handle(2) closes AND REMOVES that handle, but
        never resets _tier2_ready back to False. This safety net is
        independent of active_tier and can still fire after that
        escalation (a later tier-1 page with zero links) -- seeing
        _tier2_ready still True, it skipped re-launching a browser and
        indexed a handle that no longer existed: KeyError: 2."""
        if 2 not in self._playwright_handles:
            result = self._tier2_bootstrap(url)
            self._tier2_ready = True
            return result
        return self._tier2_fetch(url)

    def _log_escalation(self, url: str, from_tier: int, signal: str, escalating_to: str) -> None:
        tier_name = {1: "requests", 2: "stealth-headless", 3: "headed"}[from_tier]
        storage.log_breaker(self.conn, self.site_tag, url, tier_name, signal or "empty/blocked",
                             f"escalated to {escalating_to}", "pending")

    def _close_tier_handle(self, tier: int) -> None:
        handle = self._playwright_handles.pop(tier, None)
        if not handle:
            return
        playwright, browser, _context, _page = handle
        try:
            browser.close()
        except Exception as e:
            print(f"  [warning] tier {tier} browser.close() failed during cleanup for "
                  f"{self.site_tag}: {e}")
        try:
            playwright.stop()
        except Exception:
            pass

    def close(self) -> None:
        for tier in list(self._playwright_handles.keys()):
            self._close_tier_handle(tier)


# ---------------------------------------------------------------------------
# Sitemap + nav-menu auto-seed discovery
# ---------------------------------------------------------------------------

def discover_sitemap_urls(fetcher: SiteFetcher, base_url: str, max_sub_sitemaps: int = 20) -> list[str]:
    """Fetches /sitemap.xml, recursing into a sitemap_index.xml if that's
    what's found instead. Best-effort — a missing or unusable sitemap
    yields an empty list, never an error (additive input, not a
    requirement; see project plan).

    Deliberately uses fetcher._tier1_fetch() directly instead of
    fetcher.fetch() -- confirmed real, reproducible crash: JERCJKL's
    /sitemap.xml doesn't exist, and the server's own error handling
    redirects that ONE request to a completely dead domain alias
    (jercjkl.nic.in, doesn't resolve in DNS at all). Going through the
    normal fetch() escalates the site's STICKY active_tier from 1 all the
    way to 3 over a sitemap lookup nobody needs, and left the tier-3
    browser's page in a broken navigation state that then made the very
    next real fetch -- the actual homepage -- fail too
    ("interrupted by another navigation to chrome-error://chromewebdata"),
    producing a total 0-page crawl failure on a site that's perfectly
    reachable at tier 1. A sitemap is either a plain static XML file or it
    isn't -- there's no legitimate case where it needs a JS-rendering
    browser to appear -- so this never escalates tiers or touches
    Playwright at all; any failure here is just treated as "no sitemap,"
    exactly the existing degraded-but-fine outcome, without being able to
    poison the real crawl on the way there."""
    parsed = urlparse(base_url)
    sitemap_url = f"{parsed.scheme}://{parsed.netloc}/sitemap.xml"
    result = fetcher._tier1_fetch(sitemap_url)
    if not result.html:
        return []

    urls, is_index = parse_sitemap_xml(result.html)
    if not is_index:
        return urls

    all_urls = []
    for sub_sitemap_url in urls[:max_sub_sitemaps]:
        sub_result = fetcher._tier1_fetch(sub_sitemap_url)
        if sub_result.html:
            sub_urls, _ = parse_sitemap_xml(sub_result.html)
            all_urls.extend(sub_urls)
    return all_urls


def gather_auto_seeds(fetcher: SiteFetcher, base_url: str, extensions=FILE_EXTENSIONS,
                       keyword_rules=None) -> tuple[list[tuple[str, str, str]], list[str], str]:
    """Combines nav-menu links (current site structure) with sitemap.xml
    (often more complete, catches archived sections no longer in nav).
    Returns (classified_seeds, direct_document_urls, homepage_final_url) —
    sitemap entries that are ALREADY a target file extension skip seeding
    entirely and go straight to document discovery. homepage_final_url is
    normally just base_url unchanged, EXCEPT when the homepage fetch itself
    resolved somewhere else -- confirmed real, currently-relevant case:
    NERC's HTTPS vhost is broken but plain HTTP works
    (SiteFetcher._try_better_http_seed), so this fetch already transparently
    swapped to http://nerc.org.in/. Returning that here lets the caller
    queue the URL that's ACTUALLY known to work, instead of re-queuing the
    original (broken) base_url and re-triggering the exact same problem the
    very next time it's fetched -- confirmed real bug: without this, the
    swap only ever helped this one throwaway probe fetch, never the real
    crawl."""
    homepage = fetcher.fetch(base_url, require_content=True)
    nav_candidates = extract_nav_links(homepage.html, base_url) if homepage.html else []
    if not nav_candidates and homepage.html:
        # extract_nav_links() only looks inside <nav>/role="navigation"
        # containers -- confirmed real gap: NERC's real menu markup is
        # old-style (plain <div><ul><li><a>, no semantic wrapper at all),
        # so nav-specific extraction finds nothing even on a perfectly
        # real, correctly-fetched homepage. Falling back to every link on
        # the page only when nav-specific extraction found LITERALLY
        # NOTHING -- a site with real <nav> markup is completely
        # unaffected, since it never reaches this branch.
        nav_candidates = extract_links(homepage.html, homepage.final_url)

    # Confirmed real bug: HVPN's homepage (an Angular SPA with no <nav>
    # markup, triggering the fallback above) links directly to 7 real PDF
    # tenders/notices right on the homepage -- e.g.
    # hvpn.org.in:8443/profile/....pdf. Without this filter, every one of
    # those got merged into `seeds` below (pages to CRAWL) instead of
    # being recorded as documents, exactly like the sitemap path a few
    # lines below already correctly does for its own candidates. Routed
    # into the SAME direct_documents list/recording path as sitemap finds,
    # rather than adding a second, separate mechanism.
    nav_direct_documents = [url for url, _text in nav_candidates if is_document_link(url, extensions)]
    nav_candidates = [(url, text) for url, text in nav_candidates if not is_document_link(url, extensions)]

    sitemap_urls = discover_sitemap_urls(fetcher, base_url)
    direct_documents = nav_direct_documents + [u for u in sitemap_urls if is_document_link(u, extensions)]
    sitemap_page_candidates = [(u, "") for u in sitemap_urls if not is_document_link(u, extensions)]

    merged = {url: text for url, text in nav_candidates}
    for url, text in sitemap_page_candidates:
        merged.setdefault(url, text)
    merged = {url: text for url, text in merged.items() if not should_skip_asset(url)}

    classified = classify.classify_seed_candidates(list(merged.items()), keyword_rules)
    seeds = [(url, cat, sub) for url, cat, sub, _matched in classified]
    return seeds, direct_documents, homepage.final_url


# ---------------------------------------------------------------------------
# Crawl loop
# ---------------------------------------------------------------------------

@dataclass
class CrawlStats:
    pages_visited: int = 0
    documents_found: int = 0
    documents_new: int = 0
    documents_known: int = 0
    content_hashes: list = field(default_factory=list)


def crawl_site(cfg: SiteConfig, conn, fetcher: SiteFetcher, resume: bool = False) -> CrawlStats:
    stats = CrawlStats()
    rp = load_robots(cfg.seeds[0][0], conn, cfg.site_tag) if cfg.seeds else None
    keyword_rules = cfg.keyword_rules
    start_time = time.monotonic()

    visited, saved_queue = load_checkpoint(cfg.checkpoint_path) if resume else (set(), [])
    if saved_queue:
        print(f"  Resuming from checkpoint: {len(visited)} page(s) already visited.")
        queue = deque(tuple(item) for item in saved_queue)
    else:
        queue = deque()
        if cfg.seeds:
            base_url = cfg.seeds[0][0]
            print(f"  Scanning homepage nav menu + sitemap.xml for auto-seeds...")
            tier_before_seeding = fetcher.active_tier
            auto_seeds, direct_docs, homepage_final_url = gather_auto_seeds(fetcher, base_url, keyword_rules=keyword_rules)
            if tier_before_seeding != fetcher.active_tier:
                print(f"  [tier escalation] {cfg.site_tag} moved from tier {tier_before_seeding} "
                      f"to tier {fetcher.active_tier} while probing the homepage")
            if homepage_final_url != base_url:
                # Confirmed real bug otherwise: gather_auto_seeds()'s own
                # probe fetch can transparently recover a broken seed (e.g.
                # NERC's HTTPS->HTTP swap), but re-queuing the ORIGINAL
                # base_url here would just re-trigger the exact same
                # problem the moment the main loop below fetches it as an
                # ordinary page (that swap only ever applies to the
                # one-time seed probe, never an ordinary fetch). Queue the
                # URL that's actually known to work instead.
                print(f"  [seed corrected] {base_url} -> {homepage_final_url}")
            first_seed = [homepage_final_url, cfg.seeds[0][1], cfg.seeds[0][2]]
            print(f"  Found {len(auto_seeds)} auto-classified seed(s), "
                  f"{len(direct_docs)} document(s) directly in sitemap.xml.")
            all_seeds = [first_seed] + list(cfg.seeds[1:]) + [[u, c, s] for u, c, s in auto_seeds]
            for url, cat, sub in all_seeds:
                queue.append((url, 0, cat, sub))
            for doc_url in direct_docs:
                _record_document_if_new(conn, cfg, stats, doc_url, base_url, "MISC", "GENERAL", "", keyword_rules)

    pagination_counts: dict = defaultdict(int)
    robots_blocked_count = 0
    robots_would_have_blocked_count = 0
    leaf_suppressed_total = 0
    sniffed_urls: dict = {}
    print(f"  Starting crawl with {len(queue)} seed page(s) queued...")

    while queue:
        if stats.pages_visited >= cfg.max_total_pages:
            print(f"  STOPPING: hit the {cfg.max_total_pages}-page cap for this run. "
                  f"{len(queue)} page(s) were still queued and were not visited.")
            save_checkpoint(cfg.checkpoint_path, visited, list(queue))
            break

        url, depth, cat_hint, sub_hint = queue.popleft()
        url_netloc = urlparse(url).netloc
        if (url in visited or is_same_base_domain(url_netloc, cfg.excluded_domains)
                or not (url_netloc in cfg.allowed_domains
                        or is_same_base_domain(url_netloc, cfg.allowed_domains))):
            # Re-checked here (not just at enqueue time) specifically so
            # excluded_domains also applies retroactively to a URL already
            # sitting in an OLD checkpoint's queue from before that
            # exclusion existed -- confirmed real need: karnatakagov's
            # checkpoint already has thousands of BESCOM/HESCOM/GESCOM/
            # MESCOM/CESC-Mysore URLs queued from before excluded_domains
            # was added. Resuming (not --fresh) skips them here instead of
            # needing to discard the checkpoint and lose real progress.
            continue
        if not is_allowed_by_robots(rp, url):
            if cfg.respect_robots:
                storage.log_breaker(conn, cfg.site_tag, url, "n/a", "robots.txt block", "skipped", "not fetched")
                robots_blocked_count += 1
                print(f"  [robots-blocked] {url}")
                continue
            else:
                # Not logged to BREAKERS_LOG -- this fires on nearly every
                # page of most sites (confirmed: 30,947 of 34,194 total
                # breaker rows, 91%, were this single line, drowning out
                # the rows that actually matter) and robots.txt is
                # deliberately not enforced by default anyway, so there is
                # nothing here to act on. Still counted for the
                # run-summary line below.
                robots_would_have_blocked_count += 1

        visited.add(url)
        tier_before = fetcher.active_tier
        result = fetcher.fetch(url)
        if tier_before != fetcher.active_tier:
            print(f"  [tier escalation] {cfg.site_tag} moved from tier {tier_before} to tier {fetcher.active_tier}")
        if result.html is None:
            print(f"  [skip] fetch failed for {url} ({result.error or 'no content'})")
            continue

        refreshed = check_and_refresh_session(fetcher, cfg, url, result)
        if refreshed is not result:
            print(f"  [session refreshed] {url} looked like a stale-session fallback page, re-fetched after refresh")
            storage.log_breaker(conn, cfg.site_tag, url, "n/a",
                                 "stale-session fallback page detected", "refreshed listing page and retried",
                                 "succeeded")
            result = refreshed

        if cfg.postback_documents:
            # Site-specific, opt-in only -- see sbpdcl_postback_discovery.py.
            # Must run here, immediately after this exact page fetch, since
            # the ASP.NET viewstate tokens it needs are tied to this one
            # render and can't be replayed from a later, separate download
            # phase.
            import sbpdcl_postback_discovery
            new_postback_docs = sbpdcl_postback_discovery.discover_and_download(
                conn, cfg, fetcher._session, result.final_url, result.html, FILE_EXTENSIONS)
            if new_postback_docs:
                stats.documents_found += new_postback_docs
                stats.documents_new += new_postback_docs
                print(f"  [postback] {new_postback_docs} document(s) recovered from {url}")

        links = extract_links(result.html, result.final_url)
        # Per-page JS-shell safety net: a tier-1 result with zero real
        # links is often a client-side-rendered app shell rather than
        # genuinely bad content (confirmed real case: GRID-INDIA's daily
        # PSP report page, a pure React SPA route whose real document
        # links only exist after JS runs) -- verify with tier 2 before
        # accepting it as-is. Deliberately does NOT touch active_tier or
        # affect any other page on this site -- see
        # SiteFetcher.verify_empty_page_with_tier2()'s docstring for why
        # this must stay separate from the sticky escalation state
        # machine used everywhere else.
        if result.tier_used == 1 and not links:
            verified = fetcher.verify_empty_page_with_tier2(url)
            verified_links = extract_links(verified.html, verified.final_url) if verified.html else []
            if verified_links:
                print(f"  [js-shell verified] {url} looked empty at tier 1, using tier 2 instead")
                result = verified
                links = verified_links

        # Added after the JS-shell safety net above (deliberately) -- that
        # check is specifically about <a href> links going missing on a
        # client-rendered page, and frame links are a different, unrelated
        # signal that shouldn't affect it either way. See
        # extract_frame_links()'s docstring for the confirmed real case
        # (BSPHCL) this covers: an old-style frameset site whose real
        # navigation and content live inside iframe/frame src pages, never
        # reachable through this page's own <a href> links at all.
        links = links + extract_frame_links(result.html, result.final_url)

        stats.pages_visited += 1
        elapsed_min = (time.monotonic() - start_time) / 60
        print(f"  [{stats.pages_visited}] ({elapsed_min:.1f} min elapsed, {len(queue)} queued, "
              f"{stats.documents_found} docs so far) {url}")
        stats.content_hashes.append(hashlib.md5(result.html.encode("utf-8", "ignore")).hexdigest())
        time.sleep(THROTTLE_SECONDS)

        leaf = is_leaf_url(url)
        suppressed_link_count = 0
        for embedded_url in extract_embedded_document_urls(result.html, base_url=result.final_url):
            _record_document_if_new(conn, cfg, stats, embedded_url, url, cat_hint, sub_hint, "", keyword_rules)

        for link_url, link_text in links:
            if is_document_link(link_url):
                # Document recording never applies the same domain
                # restriction that page-following always respects — a real
                # ministry site commonly links to files hosted on a shared
                # government asset/CDN server it doesn't otherwise control
                # (GRID-INDIA's webcdn.grid-india.in, ERCKERALA's
                # dev.erckerala.org). is_same_base_domain() always trusts a
                # same-site subdomain regardless of domain_blind_documents;
                # that flag only extends trust further, to a domain that
                # isn't even a subdomain of this site's own domain -- an
                # explicit opt-in for the rarer, more security-sensitive
                # case (see is_same_base_domain()'s docstring).
                netloc = urlparse(link_url).netloc
                if (not is_same_base_domain(netloc, cfg.excluded_domains)
                        and (netloc in cfg.allowed_domains or is_same_base_domain(netloc, cfg.allowed_domains)
                             or cfg.domain_blind_documents)):
                    _record_document_if_new(conn, cfg, stats, link_url, url, cat_hint, sub_hint, link_text, keyword_rules)
                continue
            path_suffix = Path(urlparse(link_url).path).suffix.lower()
            is_bare_domain_root = urlparse(link_url).path in ("", "/")
            is_truly_extensionless = path_suffix == "" and not is_bare_domain_root
            # Narrower than "any non-asset extension" (confirmed too broad
            # below) -- only an ambiguous extension on a path that actually
            # looks like a file-serving handler is worth sniffing.
            is_suspected_document_handler = (
                path_suffix not in ("", *SKIP_EXTENSIONS)
                and any(kw in link_url.lower() for kw in DOCUMENT_HANDLER_PATH_KEYWORDS)
            )
            if is_truly_extensionless or is_suspected_document_handler:
                # Bare-root exclusion confirmed real, distinct case: a hub
                # site like karnataka.gov.in or saubhagya.gov.in links out
                # to dozens/hundreds of OTHER departments' own sites, each
                # as a plain https://example.gov.in with no path at all --
                # 77 of 128 sniff attempts on one karnataka.gov.in crawl
                # were exactly this, each one a slow, failing HEAD request
                # (many of these smaller government sites are themselves
                # slow/unreliable) spent confirming something already known
                # with near-certainty: nobody serves a downloadable file at
                # a site's bare root with zero path, that's always a
                # homepage. Costs nothing on a site that doesn't link out
                # this way, same as the SKIP_EXTENSIONS check above.
                #
                # is_suspected_document_handler confirmed real, distinct
                # case from the plain extensionless one this check used to
                # require: SSNNL serves real documents at
                # download.aspx?couchdbdocid=<id> (confirmed via HEAD:
                # Content-Disposition: attachment;filename=...pdf). ".aspx"
                # IS a non-empty suffix, so the original "if not suffix"
                # check skipped the sniff entirely before it ever got a
                # chance to see that header -- 103 real documents on one
                # crawl fell straight through to being queued as ordinary
                # pages instead, each one crashing with "Download is
                # starting" the way the original GERC case did. The FIRST
                # fix for this broadened the trigger to "any extension that
                # isn't a known asset type" -- confirmed too broad in turn:
                # TGERC's homepage alone has 41 ordinary .php navigation
                # links (Archive.php, currentyearorders.php, ...), each now
                # paying a needless HEAD request before being allowed
                # through as a page, adding real, compounding overhead
                # across a whole crawl and starving it of time to ever
                # reach those pages' own hundreds of real documents.
                # DOCUMENT_HANDLER_PATH_KEYWORDS narrows this back down to
                # only a path that actually looks like a file-serving
                # handler (download.aspx, getfile.php, ...), not every
                # ordinary .php/.aspx page a traditional government site is
                # built from. Cached per-run so the same URL, if linked
                # from many pages, is only ever sniffed once -- and a URL
                # already confirmed as a document must still be treated as
                # one on every later encounter, not just the first.
                if link_url not in sniffed_urls:
                    sniffed = fetcher.sniff_content_type(link_url)
                    if sniffed is None:
                        # Both the normal AND fallback HEAD requests failed
                        # outright (network error, not just "not a
                        # document") -- silently treated as "not a
                        # document" before this fix, exactly the class of
                        # gap the NRLDC bug was. Now traceable instead of
                        # invisible if it happens again on a future site.
                        storage.log_breaker(conn, cfg.site_tag, link_url, "n/a",
                                             "extensionless link could not be sniffed (HEAD request failed)",
                                             "treated as an ordinary page, not a document",
                                             "flagged for manual review")
                        content_type, content_disposition = "", ""
                    else:
                        content_type, content_disposition = sniffed
                    # Content-Type is checked first since it's the more
                    # standard signal; Content-Disposition's filename is
                    # the fallback for a server that sends a real
                    # attachment but a useless/missing Content-Type (see
                    # sniff_content_type()'s docstring -- UPJVN).
                    extension = (guess_extension_from_content_type(content_type)
                                 or guess_extension_from_content_disposition(content_disposition))
                    sniffed_urls[link_url] = bool(extension)
                if sniffed_urls[link_url]:
                    netloc = urlparse(link_url).netloc
                    if (not is_same_base_domain(netloc, cfg.excluded_domains)
                            and (netloc in cfg.allowed_domains or is_same_base_domain(netloc, cfg.allowed_domains)
                                 or cfg.domain_blind_documents)):
                        _record_document_if_new(conn, cfg, stats, link_url, url, cat_hint, sub_hint, link_text, keyword_rules)
                    continue
            link_netloc = urlparse(link_url).netloc
            if is_same_base_domain(link_netloc, cfg.excluded_domains):
                # Opt-in per site via excluded_domains in that site's config
                # -- confirmed real need: karnataka.gov.in links out to 365+
                # other department/agency subdomains, several of which
                # (the electricity DISCOMs: BESCOM, HESCOM, GESCOM, MESCOM,
                # CESC Mysore) are already being crawled as their own
                # separate, dedicated sites. Without this, karnataka.gov.in's
                # crawl would wastefully re-crawl all of them a second time.
                # Checked before allowed_domains below so an exclusion always
                # wins even if the domain would otherwise be in scope.
                continue
            if not (link_netloc in cfg.allowed_domains or is_same_base_domain(link_netloc, cfg.allowed_domains)):
                # Page-following used to be stricter than document
                # recording: is_same_base_domain() already automatically
                # trusted a document link on the same site's own subdomain
                # (see its docstring), but this exact-match-only check
                # still refused to ever CRAWL INTO one -- confirmed real,
                # recurring case: ERLDC's actual "Reports" section is
                # entirely hosted on report.erldc.in, a subdomain of
                # erldc.in that was never followed, same pattern as
                # GRID-INDIA's webcdn and ERCKERALA's dev subdomain. This
                # was the exact "notice it site by site" gap the user
                # explicitly rejected -- extending the SAME already-tested,
                # already-safe same-base-domain check (still correctly
                # excludes a genuinely unrelated domain, e.g. ERLDC's
                # fc.posoco.in, a different organization entirely) to
                # page-following closes it for every site automatically,
                # not just document recording.
                continue
            if should_skip_asset(link_url):
                continue
            if is_utility_url(link_url):
                continue
            if link_url in visited:
                continue
            # Pagination is checked BEFORE the leaf check, deliberately: a
            # leaf page (2+ query params, e.g. a filtered dashboard) still
            # gets its FURTHER filter-combination links suppressed, but a
            # genuine "next page" link must survive even if the current
            # page's own URL happens to combine a filter with a page
            # number (e.g. "?type=report&page=3"). Checking leaf first
            # was silently dropping real pagination on any listing page
            # that combined a category filter with page numbers — found
            # from a real gap between this crawler's NITI results and an
            # older tool's.
            if is_pagination_link(link_url, link_text):
                if pagination_counts[url] < DEFAULT_MAX_PAGES_PER_LISTING:
                    pagination_counts[url] += 1
                    queue.append((link_url, depth, cat_hint, sub_hint))
                continue
            if leaf:
                suppressed_link_count += 1
                continue
            if depth < cfg.max_depth:
                new_cat, new_sub, _ = classify.classify_url(link_url, link_text, cat_hint, sub_hint, keyword_rules)
                queue.append((link_url, depth + 1, new_cat, new_sub))

        if suppressed_link_count > 0:
            storage.log_breaker(conn, cfg.site_tag, url, "n/a", "leaf-url triggered",
                                 f"suppressed {suppressed_link_count} non-pagination link(s)", "not followed")
            leaf_suppressed_total += suppressed_link_count

        if stats.pages_visited % 20 == 0:
            save_checkpoint(cfg.checkpoint_path, visited, list(queue))
            print(f"  (checkpoint saved at {stats.pages_visited} pages)")

    if leaf_suppressed_total > 0:
        print(f"  [info] {leaf_suppressed_total} link(s) across all leaf (filter-heavy) pages this run "
              f"were never followed — this is the combinatorial-dashboard protection working as intended, "
              f"not a sign of missed content, but check BREAKERS_LOG.xlsx (signal: 'leaf-url triggered') "
              f"if this number looks unexpectedly large for this site.")

    if robots_blocked_count > 0:
        print(f"  [warning] robots.txt blocked {robots_blocked_count} URL(s) this run and they were "
              f"never fetched (respect_robots is on for this site). If this is most/all of what was "
              f"queued, robots.txt for this site likely disallows everything under the paths you're "
              f"crawling — reconsider whether respect_robots should be on for this site.")
    if robots_would_have_blocked_count > 0:
        print(f"  [info] {robots_would_have_blocked_count} URL(s) were technically disallowed by "
              f"robots.txt but fetched anyway (robots.txt is not enforced by default). Not logged "
              f"individually to BREAKERS_LOG.xlsx — enable respect_robots for this site if you want "
              f"these actually blocked instead.")

    CRAWL_BASICALLY_FAILED_PAGE_THRESHOLD = 3
    if (not resume and len(cfg.seeds) > 1
            and stats.pages_visited < CRAWL_BASICALLY_FAILED_PAGE_THRESHOLD
            and stats.documents_found == 0):
        # Nothing currently flags a site whose crawl died almost
        # immediately (seed unreachable, whole site down from the start)
        # short of eyeballing MASTER_SUMMARY.xlsx after the fact. Guarded
        # against resumed runs (a near-finished checkpoint legitimately
        # visits few NEW pages) and single-seed sites (a deliberately tiny
        # real site shouldn't false-positive) -- requires BOTH almost no
        # pages AND zero documents found, not page count alone.
        print(f"  [warning] {cfg.site_tag} visited only {stats.pages_visited} page(s) and found "
              f"0 documents this run — this crawl likely failed almost immediately. Flagged as a breaker.")
        storage.log_breaker(conn, cfg.site_tag, cfg.seeds[0][0] if cfg.seeds else "", "n/a",
                             f"crawl visited only {stats.pages_visited} page(s), found 0 documents",
                             "site may be unreachable or seeds broken", "flagged for manual review")

    if looks_like_spa_shell(stats.content_hashes):
        print(f"  [warning] {cfg.site_tag} looks like a single-page-app shell — "
              f"most visited pages returned near-identical content. Flagged as a breaker.")
        storage.log_breaker(conn, cfg.site_tag, cfg.seeds[0][0] if cfg.seeds else "", "n/a",
                             "SPA-shell suspected (identical content across URLs)",
                             "link-following cannot fix this", "flagged for manual review")

    if cfg.checkpoint_path.exists() and not queue:
        cfg.checkpoint_path.unlink()  # crawl finished cleanly, checkpoint no longer needed

    print(f"  Crawl finished: {stats.pages_visited} page(s) visited this run. "
          f"{stats.documents_new} unique document(s) newly recorded. "
          f"({stats.documents_known} more encounter(s) were the SAME document found again via a "
          f"different link — either earlier in this run, e.g. a file linked from multiple pages, "
          f"or from a previous run if the database wasn't reset — not {stats.documents_known} "
          f"additional distinct documents.)")

    return stats


def _record_document_if_new(conn, cfg: SiteConfig, stats: CrawlStats, doc_url: str, found_on: str,
                             cat_hint: str, sub_hint: str, link_text: str, keyword_rules: list) -> None:
    existing = storage.get_document(conn, doc_url) or storage.find_by_identity(conn, doc_url, cfg.site_tag)
    if existing:
        stats.documents_known += 1
        return

    category, subcategory, needs_verification = classify.classify_url(doc_url, link_text, cat_hint, sub_hint, keyword_rules, found_on)
    original_filename = classify.get_original_filename(doc_url)
    file_type = classify.get_file_type(original_filename)
    document_date, date_basis_note = classify.extract_date(doc_url, link_text)
    language = classify.detect_language(doc_url, found_on)
    title = classify.build_document_title(link_text, original_filename)

    year = int(document_date.split("-")[0]) if document_date else None
    if year is not None and year < cfg.min_year:
        return  # pre-floor document, not recorded at all (matches multisite's SKIPPED_PRE_YEAR behavior)

    needs_verification_final = "Yes" if (needs_verification or document_date is None) else "No"

    # Dual naming scheme: tries the SERC/CERC convention first, falls back
    # to the generic SITE__CATEGORY__SUBCATEGORY__... scheme untouched
    # whenever any required field can't be resolved without guessing — see
    # classify.resolve_filename_and_folder()'s docstring. Every document
    # gets a filename and a folder path either way; nothing is ever left
    # unnamed for lack of a naming-convention match.
    new_filename, target_folder_path, naming_scheme, verification_note = classify.resolve_filename_and_folder(
        cfg.site_tag, category, subcategory, title, language, document_date, original_filename,
        doc_url, link_text, found_on, cfg.folder_map,
    )
    new_filename = storage.disambiguate_filename(conn, new_filename, doc_url, cfg.site_tag)
    new_filename = enforce_filename_component_limit(new_filename)
    target_folder_path = enforce_folder_component_limit(target_folder_path)
    if verification_note:
        date_basis_note = f"{date_basis_note}; {verification_note}"

    storage.insert_document(conn, {
        "doc_url": doc_url, "site_tag": cfg.site_tag, "found_on": found_on,
        "category": category, "subcategory": subcategory, "document_title": title,
        "original_filename": original_filename, "file_type": file_type, "language": language,
        "document_date": document_date, "date_basis_note": date_basis_note,
        "new_filename": new_filename, "target_folder_path": target_folder_path,
        "needs_verification": needs_verification_final, "naming_scheme": naming_scheme,
    })
    stats.documents_found += 1
    stats.documents_new += 1


# ---------------------------------------------------------------------------
# Download phase (light bounded concurrency — download phase only, never crawling)
# ---------------------------------------------------------------------------

def _windows_long_path_prefix(path_str: str, threshold: int = 240) -> str:
    """Pure string logic, deliberately separated from _windows_long_path()
    below so it can be unit-tested on any platform — pathlib ties the
    actual Path class to the real OS (Python refuses to instantiate a
    WindowsPath on Linux even if os.name is patched), so this function is
    what actually gets verified in tests; the wrapper just handles the
    OS check and Path<->str conversion around it."""
    if len(path_str) < threshold:
        return path_str
    if path_str.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path_str.lstrip("\\")
    return "\\\\?\\" + path_str


def _windows_long_path(path: Path) -> Path:
    """Windows silently refuses to create a file path over roughly 260
    characters unless given the special extended-length \\\\?\\ prefix
    form. The SERC-pattern folder layout (site/regulations/subject/lang/)
    stacked on top of an already-long generated filename, inside a
    deeply-nested project folder, can realistically exceed that limit —
    this hasn't been observed failing yet, but it's a known, real Windows
    behavior worth guarding against before it happens on a run.

    Applied ONLY at the moment of actually touching the filesystem — the
    normal, clean-looking path is still what's stored in the database and
    shown in Excel; this is purely an internal workaround for disk I/O.
    A no-op on any non-Windows OS.
    """
    if os.name != "nt":
        return path
    resolved_str = str(path.resolve())
    prefixed_str = _windows_long_path_prefix(resolved_str)
    return Path(prefixed_str) if prefixed_str != resolved_str else path


def bucket_download_error(exc: Exception) -> str:
    """Collapses a download failure's exception text into a short, scannable
    category for BREAKERS_LOG.xlsx -- the same bucketing worked out by hand
    during the DB error analysis (crawler.db's documents.download_error is
    already one-off per-URL text; the breaker log should stay scannable
    across sites instead of repeating that)."""
    msg = str(exc)
    if "CERTIFICATE_VERIFY_FAILED" in msg:
        return "SSL cert verify failed"
    if "UNSAFE_LEGACY_RENEGOTIATION" in msg:
        return "SSL legacy renegotiation"
    if "forcibly closed" in msg or "ConnectionResetError" in msg or "Connection aborted" in msg:
        return "connection reset by remote host"
    if "Read timed out" in msg or isinstance(exc, requests.exceptions.ReadTimeout):
        return "read timeout"
    if isinstance(exc, requests.exceptions.Timeout):
        return "connect timeout"
    if "NameResolutionError" in msg or "getaddrinfo failed" in msg:
        return "DNS resolution failed"
    if "sign-in page" in msg:
        return "redirected to login page"
    if isinstance(exc, requests.exceptions.HTTPError):
        return f"HTTP error ({msg[-40:]})"
    if "empty response body" in msg:
        return "empty response body"
    if "WinError 123" in msg or "Errno 22" in msg:
        return "Windows path/filename syntax error"
    if "Max retries exceeded" in msg:
        return "max retries exceeded (other)"
    return msg[:80]


def download_one(db_path: str, cfg: SiteConfig, doc: dict, session: requests.Session, run_start_time: float,
                  fallback_session: requests.Session | None = None) -> tuple[bool, str | None]:
    """Runs inside a worker thread — opens its OWN SQLite connection rather
    than sharing one across threads (SQLite connections aren't safe to use
    from a thread other than the one that created them; this was caught by
    the local-server integration test, not assumed safe)."""
    conn = storage.get_connection(db_path)
    local_path = cfg.output_dir / doc["target_folder_path"] / doc["new_filename"]
    elapsed_min = (time.monotonic() - run_start_time) / 60
    retry_used = None  # set to a short label when a fallback/retry branch below fires
    try:
        safe_path = _windows_long_path(local_path)
        safe_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            response_cm = session.get(doc["doc_url"], timeout=30, stream=True)
        except requests.exceptions.SSLError:
            if fallback_session is None:
                raise
            # Automatic, config-free fallback -- confirmed real, recurring
            # problem across multiple unrelated government sites (AERC,
            # CSERC, MSERC, GRID-INDIA's CDN: missing issuer certificate;
            # NRLDC: legacy renegotiation refused), with no way to know in
            # advance which future site will hit it next. Only ever tried
            # AFTER the normal, fully-verified session's request has
            # already failed with an SSLError -- a site with no SSL
            # problems never touches this, zero extra cost.
            retry_used = "SSL fallback session"
            response_cm = fallback_session.get(doc["doc_url"], timeout=30, stream=True)
        except requests.exceptions.Timeout:
            # Same automatic, config-free philosophy as the SSL fallback
            # above, for a different confirmed real problem: TNERC's
            # server consistently needs longer than 30s to respond (all
            # 1088 of its failed downloads were exactly this timeout, zero
            # SSL involvement), with no way to know in advance which
            # future site will be similarly slow. Retried once with a
            # longer timeout on the SAME session -- a site that responds
            # within 30s never pays this extra cost.
            retry_used = f"extended {EXTENDED_DOWNLOAD_TIMEOUT}s timeout"
            response_cm = session.get(doc["doc_url"], timeout=EXTENDED_DOWNLOAD_TIMEOUT, stream=True)
        except requests.exceptions.ConnectionError:
            # A third confirmed real problem, distinct from both above:
            # WBSETCL resets the underlying TCP connection mid-request
            # ("Connection aborted... forcibly closed by the remote host")
            # on 70 documents, with zero SSL or timeout involvement -- a
            # transient server-side hiccup that a fresh connection (a
            # different session, same as the SSL fallback) typically
            # survives. Retried once; if the fallback session also fails,
            # this falls through to the same except Exception handling
            # below as before -- no change to how a genuine failure is
            # recorded.
            if fallback_session is None:
                raise
            retry_used = "fresh connection (fallback session)"
            response_cm = fallback_session.get(doc["doc_url"], timeout=30, stream=True)
        if response_cm.status_code == 429:
            # A fourth confirmed real problem, distinct from the three
            # above: some file-serving subdomains (e.g. ERCKERALA's
            # dev.erckerala.org, confirmed ~84% 429 rate) rate-limit
            # aggressively. Unlike SSL/connection-reset, a fresh session
            # identity doesn't help here -- 429 means "slow down", not
            # "try a different connection" -- so this waits (honoring the
            # server's own Retry-After header when given) and retries once
            # on the SAME session. A site that never gets rate-limited
            # never pays this cost.
            retry_after = response_cm.headers.get("Retry-After")
            try:
                wait_seconds = float(retry_after) if retry_after else 5.0
            except ValueError:
                wait_seconds = 5.0
            response_cm.close()
            time.sleep(wait_seconds)
            retry_used = retry_used or f"429 backoff ({wait_seconds:.0f}s wait)"
            response_cm = session.get(doc["doc_url"], timeout=30, stream=True)
        with response_cm as resp:
            if any(marker in resp.url.lower() for marker in ("/signin", "/login", "/auth")):
                raise PermissionError(
                    f"redirected to a sign-in page ({resp.url}) — this document likely lives "
                    f"behind a login the crawler has no credentials for, not a broken URL"
                )
            resp.raise_for_status()
            content = resp.content
        if not content:
            raise ValueError("empty response body")
        expected_ext = Path(doc["new_filename"]).suffix
        if not is_valid_file_content(content, expected_ext):
            raise ValueError(
                f"downloaded content doesn't look like a real {expected_ext} file "
                f"(got {len(content)} bytes starting with {content[:8]!r} — likely an "
                f"HTML wrapper/viewer page instead of the actual file, e.g. a "
                f"viewpdf.php-style URL that claims to be a document but isn't one)"
            )
        safe_path.write_bytes(content)
        sha256 = hashlib.sha256(content).hexdigest()
        storage.mark_downloaded(conn, doc["doc_url"], True, None, str(local_path), sha256)
        print(f"    [downloaded] ({elapsed_min:.1f} min elapsed) {doc['new_filename']}")
        if retry_used:
            # Visibility into how often this site actually needs the
            # automatic fallback -- previously invisible inside a plain
            # [downloaded] console line, with no way to tell "worked first
            # try" from "only worked after retrying" without re-reading
            # terminal scrollback.
            storage.log_breaker(conn, cfg.site_tag, doc["doc_url"], "download",
                                 "n/a", f"retried via {retry_used}", "succeeded (final tier)")
        return True, None
    except Exception as e:
        storage.mark_downloaded(conn, doc["doc_url"], False, str(e), None)
        action_taken = f"retried via {retry_used}, still failed" if retry_used else "no retry available for this error type"
        storage.log_breaker(conn, cfg.site_tag, doc["doc_url"], "download",
                             bucket_download_error(e), action_taken, "still failed")
        print(f"    [failed] ({elapsed_min:.1f} min elapsed) {doc['new_filename']}: {e}")
        return False, str(e)
    finally:
        conn.close()


def download_pending_documents(conn, cfg: SiteConfig, db_path: str, run_start_time: float) -> tuple[int, int]:
    rows = conn.execute(
        "SELECT * FROM documents WHERE site_tag = ? AND downloaded = 0 "
        "AND COALESCE(download_attempts, 0) < ?",
        (cfg.site_tag, storage.MAX_DOWNLOAD_ATTEMPTS),
    ).fetchall()
    docs = [dict(row) for row in rows]

    exhausted_count = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE site_tag = ? AND downloaded = 0 "
        "AND COALESCE(download_attempts, 0) >= ?",
        (cfg.site_tag, storage.MAX_DOWNLOAD_ATTEMPTS),
    ).fetchone()[0]
    if exhausted_count > 0:
        print(f"  {exhausted_count} document(s) have failed {storage.MAX_DOWNLOAD_ATTEMPTS}+ times and "
              f"will no longer be auto-retried — see the workbook for which ones and why.")

    if not docs:
        print("  No pending documents to download.")
        return 0, 0

    print(f"  Downloading {len(docs)} pending document(s), {cfg.download_concurrency} at a time...")
    # disable_ssl_verification is opt-in per site (unlike the automatic
    # fallback below) -- for a site CONFIRMED to always need it (e.g. HVPN's
    # hvpn.org.in:8443 has an invalid cert on every single request), this
    # skips paying for a doomed fully-verified attempt on every download
    # before falling back; a site with no known SSL problem leaves this
    # false and pays nothing extra.
    session = build_retry_session(allow_legacy_ssl=cfg.allow_legacy_ssl_renegotiation,
                                   disable_ssl_verification=cfg.disable_ssl_verification)
    # A single relaxed session, built once per run, used ONLY as an
    # automatic last-resort retry when the normal session's request fails
    # with an SSLError -- see download_one()'s comment. verify=False skips
    # certificate-chain validation (fixes "unable to get local issuer
    # certificate", confirmed on AERC/CSERC/MSERC/GRID-INDIA); the mounted
    # _LegacySSLAdapter (via allow_legacy_ssl=True) additionally allows
    # legacy renegotiation (fixes NRLDC's failure mode). Automatic and
    # config-free so a future, not-yet-seen site with either problem is
    # handled without anyone needing to notice and add a per-site flag.
    fallback_session = build_retry_session(allow_legacy_ssl=True, disable_ssl_verification=True)
    downloaded = failed = 0
    with ThreadPoolExecutor(max_workers=cfg.download_concurrency) as executor:
        futures = {executor.submit(download_one, db_path, cfg, doc, session, run_start_time, fallback_session): doc
                   for doc in docs}
        for future in as_completed(futures):
            doc = futures[future]
            try:
                success, _ = future.result()
                downloaded += success
                failed += not success
                if not success and (doc.get("download_attempts") or 0) + 1 >= storage.MAX_DOWNLOAD_ATTEMPTS:
                    # The moment a document crosses the retry ceiling, not
                    # every attempt leading up to it -- previously only a
                    # console count of exhausted documents, no queryable
                    # record of WHICH ones short of manually reading the
                    # per-site workbook.
                    storage.log_breaker(conn, cfg.site_tag, doc["doc_url"], "download",
                                         f"exhausted all {storage.MAX_DOWNLOAD_ATTEMPTS} retry attempts",
                                         "will no longer be auto-retried", "flagged for manual review")
            except Exception as e:
                # An unexpected crash for one document (e.g. its own DB
                # connection failed to open) must not stop the rest of the
                # batch from being attempted and counted. Previously
                # print()-only -- the document silently stayed
                # "not downloaded" with zero trace of why anywhere durable.
                storage.mark_downloaded(conn, doc["doc_url"], False, str(e), None)
                storage.log_breaker(conn, cfg.site_tag, doc["doc_url"], "download",
                                     f"unexpected worker error: {e}", "n/a", "still failed")
                print(f"  [warning] unexpected error downloading {doc['doc_url']}: {e}")
                failed += 1
    print(f"  Downloads complete: {downloaded} succeeded, {failed} failed.")
    return downloaded, failed


# ---------------------------------------------------------------------------
# Per-site + all-sites orchestration
# ---------------------------------------------------------------------------

def run_one_site(cfg: SiteConfig, mode: str, db_path: str, fresh: bool = False) -> dict:
    conn = storage.init_db(db_path)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now().isoformat(timespec="seconds")
    run_start_time = time.monotonic()

    if fresh and cfg.checkpoint_path.exists():
        cfg.checkpoint_path.unlink()
        print(f"  --fresh given: deleted existing checkpoint for {cfg.site_tag}, starting from scratch.")

    print(f"\n{'=' * 70}\nSite: {cfg.site_tag}  |  Mode: {mode}  |  Output: {cfg.output_dir}\n{'=' * 70}")

    previous_shas = storage.get_all_shas(conn, cfg.site_tag)

    crawl_duration_seconds = None
    download_duration_seconds = None

    if mode == "download-only":
        print("  --mode download-only: skipping the crawl entirely — downloading whatever "
              "documents are already known in the database from a previous inventory/download run.")
        stats = CrawlStats()
    else:
        crawl_start = time.monotonic()
        fetcher = SiteFetcher(cfg.site_tag, conn, use_fixed_js_wait=cfg.use_fixed_js_wait,
                               allow_legacy_ssl_renegotiation=cfg.allow_legacy_ssl_renegotiation)
        try:
            stats = crawl_site(cfg, conn, fetcher, resume=cfg.checkpoint_path.exists())
        finally:
            fetcher.close()
        crawl_duration_seconds = time.monotonic() - crawl_start

    try:
        downloaded = failed = 0
        change_log = []
        if mode in ("download", "download-only"):
            download_start = time.monotonic()
            downloaded, failed = download_pending_documents(conn, cfg, db_path, run_start_time)
            download_duration_seconds = time.monotonic() - download_start
            current_shas = storage.get_all_shas(conn, cfg.site_tag)
            change_log = storage.diff_shas(previous_shas, current_shas)

            # Every document not yet downloaded — from this run or any earlier
            # one — surfaced explicitly here, not just as an aggregate count.
            # It's also highlighted (red row) in the exported workbook, and
            # stays in the download queue so it's retried automatically on the
            # next --mode download run; nothing is ever dropped.
            failed_docs = storage.get_failed_documents(conn, cfg.site_tag)
            if failed_docs:
                print(f"\n  {'!' * 60}")
                print(f"  NOT ABLE TO DOWNLOAD — {len(failed_docs)} document(s). Still recorded, "
                      f"named, and highlighted red in the workbook; will retry automatically on "
                      f"the next --mode download run.")
                for doc in failed_docs:
                    print(f"    - {doc['new_filename']}")
                    print(f"      URL: {doc['doc_url']}")
                    print(f"      Reason: {doc['download_error']}")
                print(f"  {'!' * 60}")

        type_counts = storage.get_document_type_counts(conn, cfg.site_tag)
        total_documents = storage.get_total_document_count(conn, cfg.site_tag)
        storage.record_run(
            conn, cfg.site_tag, cfg.seeds[0][0] if cfg.seeds else "", mode, started_at,
            stats.pages_visited, stats.documents_found, stats.documents_new, stats.documents_known,
            downloaded, failed, type_counts,
            crawl_duration_seconds=crawl_duration_seconds,
            download_duration_seconds=download_duration_seconds,
        )

        workbook_path = storage.export_site_workbook(conn, cfg.site_tag, change_log,
                                                      output_path=WORKBOOKS_DIR / f"{cfg.site_tag}_documents.xlsx")
        storage.export_breakers_log(conn, output_path=SCRIPT_DIR / "BREAKERS_LOG.xlsx")
        master_summary_path = storage.update_master_summary(conn, cfg.site_tag,
                                                              output_path=MASTER_REPORTS_DIR / "MASTER_SUMMARY.xlsx")
        print(f"  {'=' * 50}")
        print(f"  TOTAL DOCUMENTS FOUND for {cfg.site_tag}: {total_documents}  "
              f"({stats.documents_new} new this run)")
        if type_counts:
            breakdown = ", ".join(f"{(ext or 'no-extension').upper()}: {count}"
                                   for ext, count in sorted(type_counts.items(), key=lambda kv: -kv[1]))
            print(f"    By file type -> {breakdown}")
        if mode in ("download", "download-only"):
            status = storage.get_download_status_counts(conn, cfg.site_tag)
            print(f"  This run attempted {downloaded + failed} pending document(s): "
                  f"{downloaded} succeeded, {failed} failed.")
            print(f"  CUMULATIVE across all runs -> Downloaded: {status['downloaded']}  "
                  f"Failed: {status['failed']}  Still pending: {status['pending']}  "
                  f"(out of {total_documents} total)")
        else:
            status = None
        print(f"  {'=' * 50}")
        print(f"  Workbook written: {workbook_path}")
        print(f"  Master summary updated: {master_summary_path}")

        return {
            "site_tag": cfg.site_tag, "pages_visited": stats.pages_visited,
            "total_documents_known": total_documents, "documents_new_this_run": stats.documents_new,
            "downloaded_this_run": downloaded, "failed_this_run": failed,
            "total_downloaded": status["downloaded"] if status else None,
            "total_failed": status["failed"] if status else None,
            "total_pending": status["pending"] if status else None,
            "workbook_path": str(workbook_path),
        }
    finally:
        conn.close()


def run_all_sites(configs: list[SiteConfig], mode: str, db_path: str, max_parallel_sites: int, fresh: bool = False) -> list[dict]:
    """Cross-site parallelism: each site runs fully sequentially/politely
    against its own domain, but multiple independent sites run at once.
    This is the biggest available speed lever with no added bot-detection
    risk, since no single site sees more concurrent traffic than before."""
    summaries = []
    with ThreadPoolExecutor(max_workers=max_parallel_sites) as executor:
        futures = {executor.submit(run_one_site, cfg, mode, db_path, fresh): cfg for cfg in configs}
        for future in as_completed(futures):
            cfg = futures[future]
            try:
                summaries.append(future.result())
            except Exception as e:
                # One site crashing (e.g. a workbook save failing, a
                # browser driver dying) must not discard every OTHER
                # site's result, and must not skip the rollup step below
                # for the sites that did succeed.
                print(f"  [warning] site {cfg.site_tag} failed with an unhandled error: {e}")

    conn = storage.init_db(db_path)
    storage.export_master_rollup(conn, [cfg.site_tag for cfg in configs],
                                  output_path=MASTER_REPORTS_DIR / f"master_summary_{datetime.now():%Y%m%d_%H%M%S}.xlsx")
    conn.close()
    return summaries


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Crawl and download documents from one or many configured sites.")
    p.add_argument("--config", default=None,
                   help="Bare site name (sites/<n>.json), a path, 'all', or a comma-separated "
                        "list of site names (e.g. 'aerc,aperc,berc') to run just that subset.")
    p.add_argument("--config-file", default=None,
                   help="Path to a text file with one site name per line — a saved, reusable "
                        "batch (e.g. 'regulators.txt'). Blank lines and lines starting with # "
                        "are ignored. Runs exactly those sites, same as the comma-list form.")
    p.add_argument("--mode", choices=["inventory", "download", "download-only"], default="inventory")
    p.add_argument("--db", default=storage.DEFAULT_DB_PATH, help="Shared SQLite database path.")
    p.add_argument("--max-pages", type=int, default=None)
    p.add_argument("--max-depth", type=int, default=None, help="Max hops from a seed page (default: 6, set per-site in the JSON config).")
    p.add_argument("--min-year", type=int, default=None)
    p.add_argument("--ignore-robots", action="store_true",
                   help="No longer needed by default — robots.txt is not enforced unless "
                        "--respect-robots is passed. Kept for explicitness/backward compatibility.")
    p.add_argument("--respect-robots", action="store_true",
                   help="Opt IN to actually enforcing robots.txt for this run (default: checked and "
                        "logged, but not enforced — see the README changelog for why).")
    p.add_argument("--fresh", action="store_true",
                   help="Ignore/delete any existing checkpoint and start this site's crawl over from "
                        "scratch. Use this after updating crawler.py itself, since an old checkpoint "
                        "can carry forward queue entries a newer version would never have queued.")
    p.add_argument("--parallel-sites", type=int, default=5, help="Max sites crawled concurrently with --config all.")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--init-config", action="store_true")
    p.add_argument("--run", action="store_true")
    p.add_argument("--site-tag")
    p.add_argument("--base-url")
    p.add_argument("--output-dir")
    p.add_argument("--seed", action="append", default=[])
    p.add_argument("--batch-add", metavar="CSV_PATH")
    return p.parse_args()


def _apply_overrides(cfg: SiteConfig, args) -> SiteConfig:
    if args.max_pages is not None:
        cfg.max_total_pages = args.max_pages
    if args.max_depth is not None:
        cfg.max_depth = args.max_depth
    if args.min_year is not None:
        cfg.min_year = args.min_year
    if args.respect_robots:
        cfg.respect_robots = True
    elif args.ignore_robots:
        cfg.respect_robots = False
    return cfg


def _read_config_file(path: str) -> list[str]:
    """One site name per line — meant to be a saved, reusable batch (e.g.
    'regulators.txt'), unlike a comma-separated list you'd retype each
    time. Blank lines and lines starting with # are ignored, so a batch
    file can have comments and spacing for readability."""
    file_path = Path(path)
    if not file_path.exists():
        raise SystemExit(f"--config-file '{path}' does not exist.")
    names = [line.strip() for line in file_path.read_text().splitlines()
              if line.strip() and not line.strip().startswith("#")]
    if not names:
        raise SystemExit(f"--config-file '{path}' has no site names in it "
                          f"(only blank lines/comments).")
    return names


def _resolve_named_configs(names: list[str], args) -> list[SiteConfig]:
    """Shared by the comma-list and --config-file paths. Every name is
    resolved BEFORE any site runs, so one typo fails clearly up front —
    naming exactly which site was bad, via resolve_config_path's own
    error message — instead of a partial run or a silent skip."""
    return [_apply_overrides(load_site_config(resolve_config_path(name)), args) for name in names]


def _print_batch_summary(summaries: list[dict]) -> None:
    for s in summaries:
        print(f"{s['site_tag']:<16} pages={s['pages_visited']:<6} "
              f"total_docs={s['total_documents_known']:<6} new_this_run={s['documents_new_this_run']:<6} "
              f"total_downloaded={s['total_downloaded']}  total_failed={s['total_failed']}")


def main():
    args = parse_args()

    if args.batch_add:
        created, skipped = batch_add_sites(Path(args.batch_add))
        print(f"Created {len(created)} config(s); skipped {len(skipped)} (already existed).")
        return

    if args.init_config:
        missing = [n for n, v in [("--site-tag", args.site_tag), ("--base-url", args.base_url),
                                   ("--output-dir", args.output_dir)] if not v]
        if missing:
            raise SystemExit(f"--init-config also needs: {', '.join(missing)}")
        out_path = write_init_config(args.site_tag, args.base_url, args.output_dir, args.seed)
        print(f"Wrote {out_path}")
        if not args.run:
            return
        cfg = _apply_overrides(load_site_config(out_path), args)
        summary = run_one_site(cfg, args.mode, args.db, args.fresh)
        print(json.dumps(summary, indent=2))
        return

    if args.config_file:
        # A saved, reusable batch -- e.g. --config-file regulators.txt --
        # for a set of sites you'll run again later, without retyping a
        # comma list or risking a stale/mistyped one each time.
        names = _read_config_file(args.config_file)
        configs = _resolve_named_configs(names, args)
        summaries = run_all_sites(configs, args.mode, args.db, args.parallel_sites, args.fresh)
        _print_batch_summary(summaries)
        return

    if args.config is None:
        raise SystemExit("Pass --config <site>, --config all, --config-file <path>, --init-config, or --batch-add.")

    if args.config.lower() == "all":
        configs = [_apply_overrides(load_site_config(p), args) for p in list_all_configs()]
        if not configs:
            raise SystemExit(f"No site configs found in {SITES_DIR}.")
        summaries = run_all_sites(configs, args.mode, args.db, args.parallel_sites, args.fresh)
        _print_batch_summary(summaries)
        return

    if "," in args.config:
        # A comma-separated subset, e.g. --config aerc,aperc,apserc,berc —
        # for a genuine one-off. For anything you'll run again, prefer
        # --config-file instead, which saves the list to a real file.
        site_names = [s.strip() for s in args.config.split(",") if s.strip()]
        if not site_names:
            raise SystemExit(f"--config '{args.config}' had commas but no site names in it.")
        configs = _resolve_named_configs(site_names, args)
        summaries = run_all_sites(configs, args.mode, args.db, args.parallel_sites, args.fresh)
        _print_batch_summary(summaries)
        return

    cfg = _apply_overrides(load_site_config(resolve_config_path(args.config)), args)
    summary = run_one_site(cfg, args.mode, args.db, args.fresh)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()