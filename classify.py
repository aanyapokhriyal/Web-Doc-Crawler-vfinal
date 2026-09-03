"""
classify.py — turns a discovered URL into a category, subcategory, date,
language, and standardized filename. No network calls, no database access —
everything here is a pure function operating on data crawler.py hands it.
That split matters for testing: everything in this file can be verified
completely offline, the way test_classify.py does.

Where each piece comes from (per project's "best of" comparison):
  - Category/subcategory classification (BASE_KEYWORD_RULES) and date
    extraction (extract_date) — ported from multisite_crawler's classify()
    and extract_date(), unchanged in behavior.
  - Language detection (LANGUAGE_CODES, detect_language) — ported from
    web-doc-crawler, which covers 12 languages by 2-letter code AND full
    name; multisite's own version only ever detected English/Hindi.
  - Filename generation, target-folder-path, slugify — web-doc-crawler's
    approach, since it was the more rigorous of the two.

A NOTE ON A BUG FOUND WHILE PORTING BASE_FOLDER_MAP:
multisite's original FOLDER_MAP hardcoded "/EN" onto exactly one entry
(ANNUALREPORT/MINISTRY -> "01_ANNUAL-REPORTS/EN") and left every other
entry with no language subfolder at all (e.g. GAZETTE ->
"07_GAZETTE-NOTIFICATIONS", no language split, and worse, the one
hardcoded "/EN" would have been wrong for an actual Hindi annual report).
That inconsistency is NOT carried forward here. BASE_FOLDER_MAP below
holds bare category paths only; get_target_folder_path() appends the
document's *actual detected* language uniformly, for every category,
every time.
"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlparse, unquote

# ---------------------------------------------------------------------------
# Category/subcategory taxonomy (from multisite_crawler) — shared across
# every site's config. A site's own "extra_keyword_rules" are merged in
# ahead of these (checked first) via merge_keyword_rules(), so a site can
# override or specialize without touching this file.
# ---------------------------------------------------------------------------

BASE_KEYWORD_RULES = [
    (r"rdss|revamped-distribution", ("SCHEME", "RDSS")),
    (r"samarth", ("SCHEME", "SAMARTH")),
    (r"saubhagya", ("SCHEME", "SAUBHAGYA")),
    (r"ddugjy|deen-dayal", ("SCHEME", "DDUGJY")),
    (r"\buday\b", ("SCHEME", "UDAY")),
    (r"psdf", ("TENDER", "PSDF")),
    (r"tbcb|transmission.*bid|bid.*transmission", ("TENDER", "TBCB-TRANSMISSION")),
    (r"rfp|tender", ("TENDER", "PROCUREMENT")),
    (r"annual-report|annual_report|annualreport", ("ANNUALREPORT", "MINISTRY")),
    (r"electricity-act|electricity_act", ("ACT", "ELECTRICITY-ACT-2003")),
    (r"energy-conservation|energy_conservation", ("ACT", "ENERGY-CONSERVATION-ACT")),
    (r"\bact\b|\brules?\b", ("ACT", "GENERAL")),
    (r"rajya.?sabha.*unstar|rs.*unstar", ("PARLIAMENT", "RAJYASABHA-UNSTARRED")),
    (r"rajya.?sabha.*star", ("PARLIAMENT", "RAJYASABHA-STARRED")),
    (r"lok.?sabha.*unstar|ls\d{8}", ("PARLIAMENT", "LOKSABHA-UNSTARRED")),
    (r"lok.?sabha.*star", ("PARLIAMENT", "LOKSABHA-STARRED")),
    (r"gazette", ("GAZETTE", "GENERAL")),
    (r"circular", ("CIRCULAR", "GENERAL")),
    (r"press.?release", ("PRESSRELEASE", "GENERAL")),
    (r"proactive.?disclosure|\brti\b", ("RTI", "PROACTIVE-DISCLOSURE")),
    (r"psu|public.?sector.?undertaking", ("PSUREPORT", "GENERAL")),
    (r"publication|working.?paper|occasional.?paper|journal|thematic.?paper|"
     r"evaluation.?stud|best.?practice|compendium|methodology|toolkit", ("PUBLICATION", "GENERAL")),
]

# Bare category paths only — NO language segment baked in here (see module
# docstring). get_target_folder_path() appends the real language uniformly.
BASE_FOLDER_MAP = {
    ("ANNUALREPORT", "MINISTRY"): "01_ANNUAL-REPORTS",
    ("ACT", "ELECTRICITY-ACT-2003"): "02_ACTS-AND-RULES/ELECTRICITY-ACT-2003",
    ("ACT", "ENERGY-CONSERVATION-ACT"): "02_ACTS-AND-RULES/ENERGY-CONSERVATION-ACT",
    ("ACT", "GENERAL"): "02_ACTS-AND-RULES/GENERAL",
    ("SCHEME", "RDSS"): "03_SCHEMES/RDSS",
    ("SCHEME", "SAMARTH"): "03_SCHEMES/SAMARTH",
    ("SCHEME", "UDAY"): "03_SCHEMES/UDAY",
    ("SCHEME", "SAUBHAGYA"): "03_SCHEMES/SAUBHAGYA",
    ("SCHEME", "DDUGJY"): "03_SCHEMES/DDUGJY",
    ("TENDER", "PSDF"): "04_TENDERS/PSDF",
    ("TENDER", "PROCUREMENT"): "04_TENDERS/PROCUREMENT",
    ("TENDER", "TBCB-TRANSMISSION"): "04_TENDERS/TBCB-TRANSMISSION",
    ("RTI", "PROACTIVE-DISCLOSURE"): "05_RTI/PROACTIVE-DISCLOSURE",
    ("RTI", "CPIO"): "05_RTI/CPIO",
    ("RTI", "ANNUAL-RETURN"): "05_RTI/ANNUAL-RETURN",
    ("RTI", "TRAINING-MATERIAL"): "05_RTI/TRAINING-MATERIAL",
    ("PARLIAMENT", "LOKSABHA-STARRED"): "06_PARLIAMENT-QA/LOKSABHA-STARRED",
    ("PARLIAMENT", "LOKSABHA-UNSTARRED"): "06_PARLIAMENT-QA/LOKSABHA-UNSTARRED",
    ("PARLIAMENT", "RAJYASABHA-STARRED"): "06_PARLIAMENT-QA/RAJYASABHA-STARRED",
    ("PARLIAMENT", "RAJYASABHA-UNSTARRED"): "06_PARLIAMENT-QA/RAJYASABHA-UNSTARRED",
    ("PARLIAMENT", "GENERAL"): "06_PARLIAMENT-QA/LOKSABHA-UNSTARRED",
    ("GAZETTE", "GENERAL"): "07_GAZETTE-NOTIFICATIONS",
    ("CIRCULAR", "GENERAL"): "08_CIRCULARS",
    ("PRESSRELEASE", "GENERAL"): "09_PRESS-RELEASES",
    ("PSUREPORT", "GENERAL"): "10_PSU-REPORTS",
    ("PUBLICATION", "GENERAL"): "12_PUBLICATIONS-REPORTS",
    ("MISC", "GENERAL"): "11_MISC-LEGACY-UPLOADS",
}
MISC_FALLBACK_FOLDER = "11_MISC-LEGACY-UPLOADS"

# Segments that carry no real classification signal on their own — dates,
# language codes, generic CMS scaffolding words, IDs, hashes. Used by the
# path-derivation fallback below so it doesn't turn "/en/uploads/2023/x.pdf"
# into a category called "EN" or "UPLOADS".
GENERIC_PATH_SEGMENTS = {
    "en", "hi", "site", "sites", "default", "files", "file", "upload",
    "uploads", "static", "assets", "download", "downloads", "docs",
    "document", "documents", "media", "content", "wp-content", "public",
    "page", "pages", "category", "categories", "topic", "topics",
    "post", "posts", "node", "view", "details", "detail", "home", "index",
}


def _is_meaningful_segment(segment: str) -> bool:
    lowered = segment.lower()
    if lowered in GENERIC_PATH_SEGMENTS:
        return False
    if re.fullmatch(r"\d+", segment):
        return False
    if re.fullmatch(r"[0-9a-f]{16,}", lowered):
        return False
    return True


def _normalize_code(segment: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", segment).upper()


def derive_category_from_path(url: str) -> tuple[str | None, str | None, bool]:
    """web-doc-crawler's technique: turn a URL's own meaningful path
    segments into a category/subcategory pair, adapting to whatever
    structure THIS site actually has — instead of forcing multisite's
    ministry-style taxonomy (TENDER/RTI/GAZETTE/SCHEME/...) onto a site
    that has none of that content (e.g. a power exchange's own sections:
    Career, Investors, Membership, Market Data). Returns (None, None,
    False) if the path had nothing meaningful in it at all, so the caller
    can fall through to a further-generic default instead.

    confident=True (2+ meaningful segments, e.g. /investors/highlights)
    means this is treated as good as a keyword match. confident=False
    (exactly 1 meaningful segment) is a weaker guess, subcategory GENERAL,
    still flagged for verification.
    """
    path = urlparse(url).path.strip("/")
    segments = [s for s in path.split("/") if s]
    # If the last segment looks like a filename (contains a dot, e.g.
    # "report.pdf"), drop it before deriving — we want the containing
    # section/page path, not the file's own slug, which is usually a
    # meaningless upload ID (this matters specifically when found_on
    # wasn't given and this function falls back to the document's OWN
    # URL — a page URL like /investors/highlights never has this problem,
    # only a document URL like /uploads/a3f9c1.pdf does).
    if segments and "." in segments[-1]:
        segments = segments[:-1]
    meaningful = [s for s in segments if _is_meaningful_segment(s)]

    if len(meaningful) >= 2:
        return _normalize_code(meaningful[0]), _normalize_code(meaningful[1]), True
    if len(meaningful) == 1:
        return _normalize_code(meaningful[0]), "GENERAL", False
    return None, None, False

# ---------------------------------------------------------------------------
# Language detection (from web-doc-crawler) — checks path segments for
# either a 2-letter code or a full language name, on BOTH the document's
# own URL and the page it was found on. Defaults to EN.
# ---------------------------------------------------------------------------

LANGUAGE_CODES = {
    "en": "EN", "hi": "HI", "te": "TE", "ta": "TA", "ml": "ML", "kn": "KN",
    "bn": "BN", "mr": "MR", "gu": "GU", "pa": "PA", "or": "OR", "as": "AS",
    "ur": "UR",
    "english": "EN", "hindi": "HI", "telugu": "TE", "tamil": "TA",
    "malayalam": "ML", "kannada": "KN", "bengali": "BN", "marathi": "MR",
    "gujarati": "GU", "punjabi": "PA", "odia": "OR", "assamese": "AS",
    "urdu": "UR",
}

DATE_ISO_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
DATE_DDMMYYYY_RE = re.compile(r"(?<!\d)(\d{2})(\d{2})(\d{4})(?!\d)")


# ---------------------------------------------------------------------------
# Config merging — a site's extra rules/folders layer on top of the base
# taxonomy without editing this file.
# ---------------------------------------------------------------------------

def merge_keyword_rules(extra_rules: list) -> list:
    """extra_rules: list of (pattern, (category, subcategory)) tuples,
    checked BEFORE the base taxonomy so a site can override or specialize."""
    return [*extra_rules, *BASE_KEYWORD_RULES]


def merge_folder_map(extra_folder_map: dict) -> dict:
    """extra_folder_map keys are (category, subcategory) tuples; entries
    here override the base map for the same key."""
    return {**BASE_FOLDER_MAP, **extra_folder_map}


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify_url(url: str, link_text: str, seed_category: str, seed_subcategory: str,
                  keyword_rules: list | None = None, found_on: str = "") -> tuple[str, str, bool]:
    """Return (category, subcategory, needs_verification). Three tiers,
    checked in order:
      1. keyword_rules (multisite's ministry-style taxonomy) — precise
         when it matches, e.g. "tender" unambiguously means TENDER.
      2. Path-derivation from the REFERRING page's URL (found_on) —
         web-doc-crawler's technique, adapts to whatever structure this
         particular site actually has. This is what makes a power
         exchange's Investors/Career/Membership sections classify
         sensibly instead of collapsing into MISC, since none of those
         have any equivalent in tier 1's ministry taxonomy.
      3. The seed's own category/subcategory hint — a last resort, only
         reached if the path itself had nothing meaningful in it either
         (pure IDs, hashes, generic words like "en"/"index").
    found_on defaults to using `url` itself when not given (e.g. when
    classifying a seed candidate, where the URL IS the page)."""
    rules = keyword_rules if keyword_rules is not None else BASE_KEYWORD_RULES
    haystack = f"{url} {link_text}".lower()
    for pattern, (category, subcategory) in rules:
        if re.search(pattern, haystack):
            return category, subcategory, False

    derived_category, derived_subcategory, confident = derive_category_from_path(found_on or url)
    if derived_category is not None:
        return derived_category, derived_subcategory, not confident

    return seed_category, seed_subcategory, True


def classify_seed_candidates(candidates: list[tuple[str, str]],
                              keyword_rules: list | None = None) -> list[tuple[str, str, str, bool]]:
    """Convenience wrapper for the nav-menu / sitemap auto-seeding step.
    candidates: list of (url, link_text) pairs pulled from a homepage's nav
    menu and/or sitemap.xml (crawler.py's job to gather these — this
    function only classifies them). Returns (url, category, subcategory,
    matched) for each — matched=False means it fell through to MISC/GENERAL
    and should still be added as a seed for coverage, just with a weaker
    hint."""
    results = []
    for url, link_text in candidates:
        category, subcategory, needs_verification = classify_url(
            url, link_text, "MISC", "GENERAL", keyword_rules
        )
        results.append((url, category, subcategory, not needs_verification))
    return results


# ---------------------------------------------------------------------------
# Date extraction (from multisite_crawler)
# ---------------------------------------------------------------------------

def extract_date(url: str, link_text: str) -> tuple[str | None, str]:
    """Return (YYYY-MM-DD or None, basis note). Checks ISO and DDMMYYYY
    patterns in the URL first, then the link text. DDMMYYYY candidates are
    validated by actually constructing the date, so e.g. "30132020" isn't
    mistaken for a real date."""
    for source, label in ((url, "URL"), (link_text, "link text")):
        if not source:
            continue
        match = DATE_ISO_RE.search(source)
        if match:
            year, month, day = match.groups()
            return f"{year}-{month}-{day}", f"Date parsed from {label} (ISO pattern)"

        match = DATE_DDMMYYYY_RE.search(source)
        if match:
            dd, mm, yyyy = match.groups()
            try:
                datetime(int(yyyy), int(mm), int(dd))
                return f"{yyyy}-{mm}-{dd}", f"Date parsed from {label} (DDMMYYYY pattern)"
            except ValueError:
                pass  # looked like a date, wasn't a valid one — keep looking

    return None, "No date found in URL or link text — open the file to confirm"


# ---------------------------------------------------------------------------
# Language detection (from web-doc-crawler)
# ---------------------------------------------------------------------------

def detect_language(doc_url: str, found_on: str = "") -> str:
    for url in (doc_url, found_on):
        if not url:
            continue
        segments = urlparse(url).path.strip("/").split("/")
        for segment in segments:
            code = LANGUAGE_CODES.get(segment.lower())
            if code:
                return code
    return "EN"


# ---------------------------------------------------------------------------
# Filename / title / folder helpers (from web-doc-crawler)
# ---------------------------------------------------------------------------

def get_original_filename(doc_url: str) -> str:
    parsed = urlparse(doc_url)
    filename = unquote(parsed.path.rsplit("/", 1)[-1])
    return filename or "unnamed_file"


def get_file_type(original_filename: str) -> str:
    return original_filename.rsplit(".", 1)[-1].lower() if "." in original_filename else ""


def build_document_title(link_text: str, original_filename: str) -> str:
    """Prefer the real link text as the title; fall back to a cleaned-up
    version of the filename when the link had no visible text (common for
    icon-only download buttons)."""
    title = (link_text or "").strip()
    if title:
        return title
    name_part = original_filename.rsplit(".", 1)[0]
    cleaned = name_part.replace("_", " ").replace("-", " ").strip()
    return cleaned or original_filename


def slugify_title(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^A-Za-z0-9\s-]", "", text or "")
    slug = re.sub(r"\s+", "-", slug.strip()).upper()
    return slug[:max_len].rstrip("-") or "UNTITLED-DOCUMENT"


def generate_filename(site_tag: str, category: str, subcategory: str, title: str,
                       language: str, document_date: str | None, original_filename: str) -> str:
    """SITE__CATEGORY__SUBCATEGORY__TITLE__LANG_DATE.ext — DATE is the
    literal string UNDATED when no date could be extracted, per the
    project's naming convention (an undated file is still filed and
    named, never skipped)."""
    extension = get_file_type(original_filename)
    title_slug = slugify_title(title)
    date_part = document_date or "UNDATED"

    filename = f"{site_tag.upper()}__{category}__{subcategory}__{title_slug}__{language}_{date_part}"
    return f"{filename}.{extension}" if extension else filename


def get_target_folder_path(category: str, subcategory: str, folder_map: dict, language: str) -> str:
    """Bare category path from folder_map, with the document's ACTUAL
    detected language appended uniformly — every category, every time (see
    module docstring re: the inconsistency this replaces).

    A category that isn't in folder_map at all (this happens for anything
    resolved via derive_category_from_path rather than the fixed
    ministry-style taxonomy — e.g. CAREER, INVESTORS on a power exchange
    site) uses its own name as the folder rather than collapsing into the
    generic MISC bucket, which would throw away exactly the specificity
    path-derivation was meant to add."""
    if (category, subcategory) in folder_map:
        base_path = folder_map[(category, subcategory)]
    else:
        base_path = f"{category}/{subcategory}"
    return f"{base_path}/{language}"


# ---------------------------------------------------------------------------
# Dual naming scheme — CERC/SERC convention (from Vishal/cerc-naming-convention.md
# and Vishal/SERC-NAMING-CONVENTION.md). Attempted first by
# resolve_filename_and_folder(); falls back to the generic scheme above
# whenever any required field can't be resolved without guessing. Nothing
# is ever left unnamed — one of the two schemes always produces a result.
# ---------------------------------------------------------------------------

REGULATORY_BODY_CODES = {
    "CERC",
    "KERC", "TNERC", "APERC", "TGERC", "MERC", "GERC", "MPERC",
    "CSERC", "RERC", "UPERC", "HERC", "PSERC", "OERC",
}

# Document-type vocabulary from both convention files' "Document types"
# section. Checked in order, most specific pattern first (e.g. CORRGAZ
# before GAZ) so a compound term isn't caught by its generic component.
DOCUMENT_TYPE_RULES = [
    (r"gazette.*corrigendum|corrigendum.*gazette", "CORRGAZ"),
    (r"effective.*date.*gazette|gazette.*effective.*date", "GAZEFF"),
    (r"effective.*date.*notif|notif\w*.*effective.*date", "NOTIEFF"),
    (r"\bdraft\b", "DFT"),
    (r"explanatory.?memorandum", "EM"),
    (r"public.?notice", "PN"),
    (r"objections?(.?(and|&).?suggestions?)?", "OBJ"),
    (r"corrigendum", "CORR"),
    (r"errata|erratum", "ERR"),
    (r"addendum", "ADD"),
    (r"detailed.?procedure|\bprocedure\b", "PROC"),
    (r"guidelines?", "GDL"),
    (r"annexure", "ANX"),
    (r"suo.?mot[ou]", "SM"),
    (r"consolidated", "CONS"),
    (r"\brepeal", "REP"),
    (r"judgment|judgement", "JDG"),
    (r"statement.?of.?reasons|\bsor\b", "SOR"),
    (r"gazette|\bgaz\b", "GAZ"),
    (r"notification|\bnoti\b", "NOTI"),
    (r"\border\b", "ORD"),
]

# Subject/family codes from cerc-naming-convention.md's family-code table
# and SERC-NAMING-CONVENTION.md's subject-code table. First match wins.
REGULATORY_SUBJECT_RULES = [
    (r"trading.?licen[cs]e", "TL"),
    (r"trading.?margin", "TM"),
    (r"otc.?platform", "OTC"),
    (r"real.?time.?market", "RTM"),
    (r"power.?market.?regulation", "PMR"),
    (r"general.?network.?access|\bgna\b", "GNA"),
    (r"cross.?border.?trade", "CBTE"),
    (r"renewable.?energy.?certificate|\brec\b", "REC"),
    (r"virtual.?ppa|\bvppa\b", "VPPA"),
    (r"carbon.?credit", "CCC"),
    (r"ancillary.?service", "AS"),
    (r"congestion", "CONG"),
    (r"energy.?saving.?certificate|escert", "ESC"),
    (r"open.?access.?registry|\bnoar\b", "NOAR"),
    (r"multi.?year.?tariff|\bmyt\b", "MYT"),
    (r"resource.?adequacy", "RA"),
    (r"forecasting.*scheduling|\bfsdsm\b", "FSDSM"),
    (r"deviation.?settlement|\bdsm\b", "DSM"),
    (r"grid.?code", "GC"),
    (r"green.?energy.?open.?access|\bgeoa\b", "GEOA"),
    (r"open.?access", "OA"),
    (r"renewable.?purchase.?obligation|\brpo\b", "RPO"),
    (r"net.?metering|rooftop.?solar", "NM"),
    (r"energy.?storage", "ESS"),
    (r"supply.?code", "SC"),
    (r"standards?.?of.?performance|\bsop\b", "SOP"),
    (r"grievance.?redressal|\bcgrf\b", "CGRF"),
    (r"licens?ing", "LIC"),
    (r"distribution.?franchisee", "DF"),
    (r"captive.?generat", "CGP"),
    (r"security.?deposit", "SD"),
    (r"cross.?subsidy.?surcharge|\bcss\b", "CSS"),
    (r"smart.?meter|prepaid.?meter", "SM"),
    (r"conduct.?of.?business|\bcob\b", "COB"),
    (r"fees?.?(and|&).?charges?", "FEE"),
]

OFFICIAL_REG_NUMBER_RE = re.compile(r"regulation\s*no\.?\s*(\d+)\s*of\s*(\d{4})", re.IGNORECASE)


def extract_official_regulation_number(text: str) -> str | None:
    """Only returns a value when the text literally names one (e.g.
    "Regulation No. 2 of 2023") — per SERC-NAMING-CONVENTION.md §5, this
    field must never be invented for a state that doesn't use one."""
    if not text:
        return None
    match = OFFICIAL_REG_NUMBER_RE.search(text)
    if not match:
        return None
    n, year = match.groups()
    return f"R{n}of{year}"


def classify_regulatory_document(site_tag: str, url: str, link_text: str, found_on: str,
                                  document_date: str | None) -> dict | None:
    """Resolves a document into the SERC naming-convention fields, or
    returns None the moment any required field can't be determined
    without guessing — the caller (resolve_filename_and_folder) falls
    back to the generic scheme in that case.

    CERC-style naming (P###/S###) is deliberately never produced here:
    those numbers only mean something when matched against CERC's own
    official Current Regulations table, which this crawler has no access
    to for a freshly discovered document. Faking one would be worse than
    not having it — see readme.md's naming-convention section."""
    tag = site_tag.upper()
    if tag not in REGULATORY_BODY_CODES or tag == "CERC":
        return None

    haystack = f"{url} {link_text}".lower()

    doc_type = next((token for pattern, token in DOCUMENT_TYPE_RULES if re.search(pattern, haystack)), None)
    if doc_type is None:
        return None

    subject = next((code for pattern, code in REGULATORY_SUBJECT_RULES if re.search(pattern, haystack)), None)
    if subject is None:
        return None

    if not document_date:
        return None  # P<YYYY> needs a real year; no year, no regulatory-style name

    return {
        "code": tag,
        "subject": subject,
        "principal_year": document_date.split("-")[0],
        "amendment": "A0",
        "official_reg_number": extract_official_regulation_number(f"{url} {link_text}"),
        "type": doc_type,
    }


def generate_regulatory_filename(fields: dict, language: str, document_date: str | None,
                                  original_filename: str) -> str:
    """<SERC>-<SUBJ>-P<YYYY>-A<n>[-R<n>of<YYYY>]-<TYPE>[-<LANG>]-<YYYYMMDD>.ext
    per SERC-NAMING-CONVENTION.md §1. Only called once
    classify_regulatory_document() has confirmed every field is real."""
    extension = get_file_type(original_filename)
    date_part = (document_date or "").replace("-", "") or "UNDATED"
    parts = [fields["code"], fields["subject"], f"P{fields['principal_year']}", fields["amendment"]]
    if fields.get("official_reg_number"):
        parts.append(fields["official_reg_number"])
    parts.append(fields["type"])
    if language and language != "EN":
        parts.append(language)
    parts.append(date_part)
    filename = "-".join(parts)
    return f"{filename}.{extension}" if extension else filename


def get_regulatory_folder_path(fields: dict, language: str) -> str:
    """Mirrors serc_curated/'s own layout (see SERC-NAMING-CONVENTION.md §9):
    <SERC>/regulations/<SUBJECT>/<LANG>/"""
    return f"{fields['code']}/regulations/{fields['subject']}/{language}"


def resolve_filename_and_folder(site_tag: str, category: str, subcategory: str, title: str,
                                 language: str, document_date: str | None, original_filename: str,
                                 url: str, link_text: str, found_on: str, folder_map: dict
                                 ) -> tuple[str, str, str, str | None]:
    """Dual-scheme entry point — tries the SERC/CERC convention first;
    whenever any required field can't be resolved without guessing, falls
    back to the original generic scheme untouched. Nothing is ever
    skipped for lack of a match — one of the two schemes always produces
    a filename and a folder path.

    Returns (filename, folder_path, naming_scheme, verification_note)
    where naming_scheme is "SERC" or "GENERIC", and verification_note is
    a caveat to append to the document's own date-basis note (only set
    for "SERC", since its amendment number always defaults to A0 —
    never None otherwise)."""
    fields = classify_regulatory_document(site_tag, url, link_text, found_on, document_date)
    if fields is not None:
        filename = generate_regulatory_filename(fields, language, document_date, original_filename)
        folder_path = get_regulatory_folder_path(fields, language)
        note = "Amendment number defaulted to A0 — not verified against an official regulation history."
        return filename, folder_path, "SERC", note

    filename = generate_filename(site_tag, category, subcategory, title, language, document_date, original_filename)
    folder_path = get_target_folder_path(category, subcategory, folder_map, language)
    return filename, folder_path, "GENERIC", None