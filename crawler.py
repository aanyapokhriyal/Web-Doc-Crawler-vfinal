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
    ".mp4", ".mp3", ".avi", ".mov", ".wmv",
}
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
PAGE_TIMEOUT_MS = 45_000
JS_SETTLE_POLL_MS = 300              # adaptive-wait poll interval
JS_SETTLE_CEILING_MS = 6_000         # never wait longer than multisite did
DEFAULT_DOWNLOAD_CONCURRENCY = 4     # download phase only, never crawling
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
    domain_blind_documents: bool = False

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
        domain_blind_documents=bool(raw.get("domain_blind_documents", False)),
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
        "domain_blind_documents": False,
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
            "domain_blind_documents: false means a document link pointing "
            "outside allowed_domains is dropped, same as a page link would "
            "be — set to true only for a site confirmed to host real "
            "documents on another domain it doesn't control page-crawling "
            "on (e.g. a shared government asset/CDN server). This ONLY "
            "affects whether a DOCUMENT gets recorded; page-following stays "
            "exactly as restricted to allowed_domains either way."
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

def load_robots(base_url: str):
    """Always attempts to load robots.txt, regardless of whether it will
    actually be enforced — SiteConfig.respect_robots controls whether a
    disallowed URL is skipped or just logged and fetched anyway, but
    either way we need the parsed rules to know which case applies."""
    parsed = urlparse(base_url)
    rp = robotparser.RobotFileParser()
    try:
        rp.set_url(f"{parsed.scheme}://{parsed.netloc}/robots.txt")
        rp.read()
        return rp
    except Exception:
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
    return urlparse(url)._replace(fragment="").geturl()


def is_document_link(url: str, extensions=FILE_EXTENSIONS) -> bool:
    return Path(urlparse(url).path).suffix.lower() in extensions


def should_skip_asset(url: str, skip_extensions=SKIP_EXTENSIONS) -> bool:
    """True for images/CSS/JS/fonts/media — never worth queuing as a page
    to crawl, and never a document to record either."""
    return Path(urlparse(url).path).suffix.lower() in skip_extensions


def is_leaf_url(url: str, threshold: int = LEAF_QUERY_PARAM_THRESHOLD) -> bool:
    """A page whose OWN query string has >= threshold parameters is treated
    as a dead end: files on it are still recorded, its links are not
    followed further. Stops combinatorial dashboard explosions (multisite's
    finding: a real powermin run took 5+ hours without this)."""
    return len(parse_qs(urlparse(url).query)) >= threshold


def is_pagination_link(abs_url: str, link_text: str) -> bool:
    return bool(PAGINATION_TEXT_RE.match(link_text or "") or PAGINATION_URL_RE.search(abs_url))


def extract_links(html: str, base_url: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        abs_url = normalize_url(urljoin(base_url, href))
        links.append((abs_url, a.get_text(strip=True)))
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
            if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            abs_url = normalize_url(urljoin(base_url, href))
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


def extract_embedded_document_urls(html: str, extensions=FILE_EXTENSIONS) -> set[str]:
    """Some JS-framework sites (Next.js/Nuxt/etc.) ship document URLs
    inside a JSON blob in the raw HTML for client-side hydration, never as
    a real <a href> — regex-scans the full raw text independent of where
    it sits in the markup. (web-doc-crawler's technique; likely the key to
    iexindia-style sites per project testing.)"""
    if not html:
        return set()
    return set(_document_url_pattern(extensions).findall(html))


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


# ---------------------------------------------------------------------------
# Checkpoint / resume (Parikshith's pattern)
# ---------------------------------------------------------------------------

def load_checkpoint(path: Path) -> tuple[set, list]:
    if not path.exists():
        return set(), []
    data = json.loads(path.read_text())
    return set(data.get("visited", [])), data.get("queue", [])


def save_checkpoint(path: Path, visited: set, queue_snapshot: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"visited": list(visited), "queue": queue_snapshot}))


# ---------------------------------------------------------------------------
# Fetch tiers
# ---------------------------------------------------------------------------

def build_retry_session(user_agent: str = USER_AGENT_PLAIN) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent})
    retry = Retry(total=4, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504],
                   allowed_methods=["GET", "HEAD"])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
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
                 tier3_bootstrap=None, tier3_fetch=None, use_fixed_js_wait: bool = False):
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
        self._session = build_retry_session()
        self._tier2_ready = False
        self._tier3_ready = False
        self._playwright_handles = {}  # tier -> (playwright, browser, context, page)

    # -- real implementations (need network / Playwright; not testable here) --

    def _real_tier1_fetch(self, url: str) -> FetchResult:
        try:
            resp = self._session.get(url, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            return FetchResult(None, url, None, 1, str(e))
        if resp.status_code in (403, 429, 503):
            return FetchResult(None, resp.url, resp.status_code, 1, f"HTTP {resp.status_code}")
        if "text/html" not in resp.headers.get("Content-Type", ""):
            return FetchResult(None, resp.url, resp.status_code, 1, "non-HTML response")
        if len(resp.text) < self.min_content_length:
            return FetchResult(None, resp.url, resp.status_code, 1, "suspiciously short content")
        return FetchResult(resp.text, resp.url, resp.status_code, 1, None)

    def _real_tier2_bootstrap(self, url: str):
        from playwright.sync_api import sync_playwright
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(user_agent=USER_AGENT_REAL, viewport={"width": 1920, "height": 1080})
        context.add_init_script(STEALTH_INIT_SCRIPT)
        page = context.new_page()
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
        # Cookies from bootstrap are already in self._session — try that first.
        result = self._real_tier1_fetch(url)
        if result.html is not None:
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
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context(user_agent=USER_AGENT_REAL)
        page = context.new_page()
        self._playwright_handles[3] = (playwright, browser, context, page)  # register before the risky goto
        try:
            response = page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            self._adaptive_settle(page)
            html = page.content()
        except Exception as e:
            return FetchResult(None, url, None, 3, str(e))
        return FetchResult(html, page.url, response.status if response else None, 3, None)

    def _real_tier3_fetch(self, url: str) -> FetchResult:
        _, _, _, page = self._playwright_handles[3]
        try:
            response = page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            self._adaptive_settle(page)
            return FetchResult(page.content(), page.url, response.status if response else None, 3, None)
        except Exception as e:
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

    def sniff_content_type(self, url: str, timeout: int = 10) -> str | None:
        """HEAD-requests a URL to check its real Content-Type without
        downloading the body — for a link with no file extension at all
        that might still be a real document. Confirmed real need from
        GERC: /viewdocument/<opaque-token> URLs serve real files but have
        no recognizable extension, so is_document_link() alone can never
        catch them — they were being queued as ordinary pages, and a real
        browser trying to "visit" one instead started a file download,
        crashing that page with "Download is starting" and never
        recording the document. Fails safe: any network error or timeout
        just means "treat this as an ordinary page," exactly matching
        today's existing behavior — a site with no extension-less links
        at all sees zero behavior change and pays zero extra cost."""
        try:
            resp = self._session.head(url, timeout=timeout, allow_redirects=True)
            return resp.headers.get("Content-Type", "")
        except Exception:
            return None

    def _evaluate_fetch(self, result: FetchResult, url: str, require_content: bool) -> tuple[bool, str]:
        """Returns (is_successful, failure_reason). When require_content is
        False (the default, used for every normal page in a crawl), this
        reduces exactly to the original "did we get any html at all" check
        — no behavior change there. When True (used only for the initial
        site-probing fetch), a page must ALSO contain at least
        MIN_MEANINGFUL_LINKS_FOR_SEED_PAGE real links to count as
        successful — see the constant's docstring for why."""
        if result.html is None:
            return False, result.error or "no content"
        if require_content:
            link_count = len(extract_links(result.html, url))
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
            storage.log_breaker(self.conn, self.site_tag, url, "headed", reason,
                                 "no further tier available", "still failed")
            return FetchResult(None, boot.final_url, boot.status_code, 3, reason)

        result = self._tier3_fetch(url)
        ok, reason = self._evaluate_fetch(result, url, require_content)
        if not ok:
            storage.log_breaker(self.conn, self.site_tag, url, "headed", reason,
                                 "no further tier available", "still failed")
            return FetchResult(None, result.final_url, result.status_code, 3, reason)
        return result

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
        except Exception:
            pass
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
    requirement; see project plan)."""
    parsed = urlparse(base_url)
    sitemap_url = f"{parsed.scheme}://{parsed.netloc}/sitemap.xml"
    result = fetcher.fetch(sitemap_url)
    if not result.html:
        return []

    urls, is_index = parse_sitemap_xml(result.html)
    if not is_index:
        return urls

    all_urls = []
    for sub_sitemap_url in urls[:max_sub_sitemaps]:
        sub_result = fetcher.fetch(sub_sitemap_url)
        if sub_result.html:
            sub_urls, _ = parse_sitemap_xml(sub_result.html)
            all_urls.extend(sub_urls)
    return all_urls


def gather_auto_seeds(fetcher: SiteFetcher, base_url: str, extensions=FILE_EXTENSIONS,
                       keyword_rules=None) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Combines nav-menu links (current site structure) with sitemap.xml
    (often more complete, catches archived sections no longer in nav).
    Returns (classified_seeds, direct_document_urls) — sitemap entries that
    are ALREADY a target file extension skip seeding entirely and go
    straight to document discovery."""
    homepage = fetcher.fetch(base_url, require_content=True)
    nav_candidates = extract_nav_links(homepage.html, base_url) if homepage.html else []

    sitemap_urls = discover_sitemap_urls(fetcher, base_url)
    direct_documents = [u for u in sitemap_urls if is_document_link(u, extensions)]
    sitemap_page_candidates = [(u, "") for u in sitemap_urls if not is_document_link(u, extensions)]

    merged = {url: text for url, text in nav_candidates}
    for url, text in sitemap_page_candidates:
        merged.setdefault(url, text)
    merged = {url: text for url, text in merged.items() if not should_skip_asset(url)}

    classified = classify.classify_seed_candidates(list(merged.items()), keyword_rules)
    seeds = [(url, cat, sub) for url, cat, sub, _matched in classified]
    return seeds, direct_documents


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
    rp = load_robots(cfg.seeds[0][0]) if cfg.seeds else None
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
            auto_seeds, direct_docs = gather_auto_seeds(fetcher, base_url, keyword_rules=keyword_rules)
            if tier_before_seeding != fetcher.active_tier:
                print(f"  [tier escalation] {cfg.site_tag} moved from tier {tier_before_seeding} "
                      f"to tier {fetcher.active_tier} while probing the homepage")
            print(f"  Found {len(auto_seeds)} auto-classified seed(s), "
                  f"{len(direct_docs)} document(s) directly in sitemap.xml.")
            all_seeds = list(cfg.seeds) + [[u, c, s] for u, c, s in auto_seeds]
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
        if url in visited or urlparse(url).netloc not in cfg.allowed_domains:
            continue
        if not is_allowed_by_robots(rp, url):
            if cfg.respect_robots:
                storage.log_breaker(conn, cfg.site_tag, url, "n/a", "robots.txt block", "skipped", "not fetched")
                robots_blocked_count += 1
                print(f"  [robots-blocked] {url}")
                continue
            else:
                storage.log_breaker(conn, cfg.site_tag, url, "n/a", "robots.txt block (ignored by default)",
                                     "fetched anyway", "logged for awareness only")
                robots_would_have_blocked_count += 1

        visited.add(url)
        tier_before = fetcher.active_tier
        result = fetcher.fetch(url)
        if tier_before != fetcher.active_tier:
            print(f"  [tier escalation] {cfg.site_tag} moved from tier {tier_before} to tier {fetcher.active_tier}")
        if result.html is None:
            print(f"  [skip] fetch failed for {url} ({result.error or 'no content'})")
            continue

        stats.pages_visited += 1
        elapsed_min = (time.monotonic() - start_time) / 60
        print(f"  [{stats.pages_visited}] ({elapsed_min:.1f} min elapsed, {len(queue)} queued, "
              f"{stats.documents_found} docs so far) {url}")
        stats.content_hashes.append(hashlib.md5(result.html.encode("utf-8", "ignore")).hexdigest())
        time.sleep(THROTTLE_SECONDS)

        leaf = is_leaf_url(url)
        suppressed_link_count = 0
        for embedded_url in extract_embedded_document_urls(result.html):
            _record_document_if_new(conn, cfg, stats, embedded_url, url, cat_hint, sub_hint, "", keyword_rules)

        for link_url, link_text in extract_links(result.html, result.final_url):
            if is_document_link(link_url):
                # Document recording can optionally ignore the domain
                # restriction that page-following always respects — a real
                # ministry site commonly links to files hosted on a shared
                # government asset/CDN server it doesn't otherwise control.
                # cfg.domain_blind_documents defaults to False, in which
                # case this is IDENTICAL to the original single domain
                # check (False or X == X) — zero behavior change for any
                # site that doesn't explicitly opt in. Confirmed from
                # multisite's own source: it recorded file hits with NO
                # domain check at all, only restricting page-following.
                if cfg.domain_blind_documents or urlparse(link_url).netloc in cfg.allowed_domains:
                    _record_document_if_new(conn, cfg, stats, link_url, url, cat_hint, sub_hint, link_text, keyword_rules)
                continue
            if not Path(urlparse(link_url).path).suffix:
                # No extension at all in the path -- worth a quick HEAD
                # check before assuming this is just an ordinary page.
                # Cached per-run so the same extension-less URL, if linked
                # from many pages, is only ever sniffed once -- and a URL
                # already confirmed as a document must still be treated as
                # one on every later encounter, not just the first.
                if link_url not in sniffed_urls:
                    content_type = fetcher.sniff_content_type(link_url)
                    sniffed_urls[link_url] = bool(content_type and guess_extension_from_content_type(content_type))
                if sniffed_urls[link_url]:
                    if cfg.domain_blind_documents or urlparse(link_url).netloc in cfg.allowed_domains:
                        _record_document_if_new(conn, cfg, stats, link_url, url, cat_hint, sub_hint, link_text, keyword_rules)
                    continue
            if urlparse(link_url).netloc not in cfg.allowed_domains:
                continue
            if should_skip_asset(link_url):
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
              f"robots.txt but fetched anyway (robots.txt is not enforced by default) — see "
              f"BREAKERS_LOG.xlsx if you want to review which ones.")

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


def download_one(db_path: str, cfg: SiteConfig, doc: dict, session: requests.Session, run_start_time: float) -> tuple[bool, str | None]:
    """Runs inside a worker thread — opens its OWN SQLite connection rather
    than sharing one across threads (SQLite connections aren't safe to use
    from a thread other than the one that created them; this was caught by
    the local-server integration test, not assumed safe)."""
    conn = storage.get_connection(db_path)
    local_path = cfg.output_dir / doc["target_folder_path"] / doc["new_filename"]
    elapsed_min = (time.monotonic() - run_start_time) / 60
    try:
        safe_path = _windows_long_path(local_path)
        safe_path.parent.mkdir(parents=True, exist_ok=True)
        with session.get(doc["doc_url"], timeout=30, stream=True) as resp:
            if any(marker in resp.url.lower() for marker in ("/signin", "/login", "/auth")):
                raise PermissionError(
                    f"redirected to a sign-in page ({resp.url}) — this document likely lives "
                    f"behind a login the crawler has no credentials for, not a broken URL"
                )
            resp.raise_for_status()
            content = resp.content
        if not content:
            raise ValueError("empty response body")
        safe_path.write_bytes(content)
        sha256 = hashlib.sha256(content).hexdigest()
        storage.mark_downloaded(conn, doc["doc_url"], True, None, str(local_path), sha256)
        print(f"    [downloaded] ({elapsed_min:.1f} min elapsed) {doc['new_filename']}")
        return True, None
    except Exception as e:
        storage.mark_downloaded(conn, doc["doc_url"], False, str(e), None)
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
    session = build_retry_session()
    downloaded = failed = 0
    with ThreadPoolExecutor(max_workers=cfg.download_concurrency) as executor:
        futures = [executor.submit(download_one, db_path, cfg, doc, session, run_start_time) for doc in docs]
        for future in as_completed(futures):
            success, _ = future.result()
            downloaded += success
            failed += not success
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

    if mode == "download-only":
        print("  --mode download-only: skipping the crawl entirely — downloading whatever "
              "documents are already known in the database from a previous inventory/download run.")
        stats = CrawlStats()
    else:
        fetcher = SiteFetcher(cfg.site_tag, conn, use_fixed_js_wait=cfg.use_fixed_js_wait)
        try:
            stats = crawl_site(cfg, conn, fetcher, resume=cfg.checkpoint_path.exists())
        finally:
            fetcher.close()

    downloaded = failed = 0
    change_log = []
    if mode in ("download", "download-only"):
        downloaded, failed = download_pending_documents(conn, cfg, db_path, run_start_time)
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

    conn.close()
    return {
        "site_tag": cfg.site_tag, "pages_visited": stats.pages_visited,
        "total_documents_known": total_documents, "documents_new_this_run": stats.documents_new,
        "downloaded_this_run": downloaded, "failed_this_run": failed,
        "total_downloaded": status["downloaded"] if status else None,
        "total_failed": status["failed"] if status else None,
        "total_pending": status["pending"] if status else None,
        "workbook_path": str(workbook_path),
    }


def run_all_sites(configs: list[SiteConfig], mode: str, db_path: str, max_parallel_sites: int, fresh: bool = False) -> list[dict]:
    """Cross-site parallelism: each site runs fully sequentially/politely
    against its own domain, but multiple independent sites run at once.
    This is the biggest available speed lever with no added bot-detection
    risk, since no single site sees more concurrent traffic than before."""
    summaries = []
    with ThreadPoolExecutor(max_workers=max_parallel_sites) as executor:
        futures = {executor.submit(run_one_site, cfg, mode, db_path, fresh): cfg for cfg in configs}
        for future in as_completed(futures):
            summaries.append(future.result())

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