# Multi-site document crawler — setup & usage

Three files do the work: `crawler.py` (crawl loop + fetch tiers + CLI),
`classify.py` (categorization/dates/language/filenames), `storage.py`
(SQLite + Excel export). All three must sit in the same folder — `crawler.py`
imports the other two directly.

## One-time setup

```bash
# 1. Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate          # Mac/Linux
venv\Scripts\activate             # Windows

# 2. Install dependencies
pip install -r requirements.txt

# 3. Install a real Chromium binary for Playwright
playwright install chromium
```

Step 3 is only actually *used* if a site needs to escalate past tier 1
(plain requests) — but install it up front anyway, since you won't know
which sites need it until you run them.

No pre-filled site configs ship with this tool, on purpose (see the
project plan doc) — every site, including the five already tested, gets
added fresh via `--init-config` or `--batch-add`.

## Running it for iexindia, step by step

**Step 1 — scaffold a config.** This writes `sites/iexindia.json` and
stops there so you can review it before anything crawls:

```bash
python3 crawler.py --init-config \
  --site-tag IEXINDIA \
  --base-url https://www.iexindia.com \
  --output-dir "./downloads/IEXINDIA"
```

**Step 2 (optional but worth doing) — open `sites/iexindia.json`** and add
any listing/archive pages you already know about to `"seeds"`. Not
required — the crawler will also auto-discover seeds from the homepage's
nav menu and sitemap.xml — but a known seed speeds up discovery of a
section that's deep or not in the main nav.

**Step 3 — inventory run first (safe, downloads nothing):**

```bash
python3 crawler.py --config iexindia --mode inventory
```

This crawls the site, classifies whatever it finds, records it in
`crawler.db`, and writes `downloads/IEXINDIA/IEXINDIA_documents.xlsx` —
but doesn't download a single file yet. Open that workbook and check it
looks right before moving on.

**Step 4 — actually download:**

```bash
python3 crawler.py --config iexindia --mode download
```

Re-crawls (fetch tier is re-detected fresh, never cached from a prior
run — see `crawler.py`'s module docstring for why), downloads anything
new or still pending, and updates the workbook's `Change_Log` tab.

**One-shot alternative** (scaffold + crawl immediately, skips the pause
after Step 1):

```bash
python3 crawler.py --init-config --run \
  --site-tag IEXINDIA \
  --base-url https://www.iexindia.com \
  --output-dir "./downloads/IEXINDIA" \
  --mode inventory
```

## Specifically worth knowing for iexindia

Per project testing, IEXINDIA serves every document as a JSON-embedded
URL, never a real `<a href>` link — this should be caught by the
JSON-regex extraction technique regardless of which fetch tier ends up
active, since testing found IEX's document URLs are visible even to a
plain requests fetch. Run the inventory pass and check the console output
for how many documents it finds and which tier it settled on before
assuming it needs escalation.

## Where output lands

| What | Where |
|---|---|
| Downloaded files, organized by category | `downloads/<SITE>/<category-folder>/` |
| Per-site workbook | `_WORKBOOKS/<SITE>_documents.xlsx` |
| Shared database (all sites) | `crawler.db` (override with `--db path`) |
| Breakers log (all sites, all runs, cumulative) | `BREAKERS_LOG.xlsx`, next to `crawler.py` |
| Master rollup across sites (always current) | `_MASTER_REPORTS/MASTER_SUMMARY.xlsx` — updates after every run, single-site or `--config all` |
| Master rollup snapshots (history) | `_MASTER_REPORTS/master_summary_<timestamp>.xlsx` — only written when you run `--config all` |

## Naming convention: dual scheme

Every discovered document gets a filename either way — nothing is ever
left unnamed or skipped because it doesn't fit a pattern.

- **SERC pattern** (regulatory documents only):
  `<SERC>-<SUBJECT>-P<YYYY>-A<n>[-R<n>of<YYYY>]-<TYPE>[-<LANG>]-<YYYYMMDD>.ext`
  — applied only when *all three* hold: the site is a recognized SERC code
  (KERC, TNERC, APERC, TGERC, MERC, GERC, MPERC, CSERC, RERC, UPERC, HERC,
  PSERC, OERC — **not** CERC, which is deliberately excluded since its
  official numbering can't be verified against CERC's own regulation
  history from a live crawl), a document `TYPE` is recognized (order,
  notification, gazette, corrigendum, etc.), a subject code is recognized
  (MYT, RPO, REC, trading license, open access, etc.), and the document
  has a real extracted date. `A<n>` always defaults to `A0` — the crawler
  has no access to an official amendment history to number it correctly,
  and never fabricates one; the workbook's Date Basis / Note column flags
  this on every SERC-pattern row.
- **GENERIC pattern** (fallback, used for everything else — tenders,
  careers, investor pages, market data, CERC entirely, and any
  regulatory-site document that doesn't cleanly match all three conditions
  above): `SITE__CATEGORY__SUBCATEGORY__DOCUMENT-TITLE__LANG_YYYY-MM-DD.ext`

Each site's exported workbook has a `Naming Scheme` column showing which
pattern each row got, and a `Naming_Convention` tab explaining both.

## When a document can't be downloaded

Nothing is ever silently dropped. A document that fails to download stays
in the database and is retried automatically on every later `--mode
download` or `--mode download-only` run — **up to 5 attempts** per
document; after that, it stops being auto-retried so a permanently broken
link doesn't waste time forever (still visible in the workbook, just no
longer re-attempted).

It's surfaced in two places so it's impossible to miss:
1. **Console**: a `NOT ABLE TO DOWNLOAD` block at the end of a download
   run, listing each one's filename, URL, and the actual error.
2. **Workbook**: that row is highlighted red in `File_Tracking`. Its
   `Download Status` cell reads `NOT DOWNLOADED — <reason>` while still
   being retried, or `NOT DOWNLOADED (gave up after 5 attempts) —
   <reason>` once the cap is hit.

## Running many sites at once

```bash
python3 crawler.py --batch-add sites_to_add_TEMPLATE.csv   # add several sites from one CSV
python3 crawler.py --config all --mode inventory           # crawl every sites/*.json
python3 crawler.py --config all --mode download            # download everything, once you're happy
```

`--config all` runs sites concurrently (default 5 at a time — override
with `--parallel-sites N`). Each site still crawls its own domain fully
sequentially/politely; only *different* sites run in parallel with each
other, so no single site sees more traffic than a solo run would produce.

### The batch-add CSV, in full

The CSV needs exactly this header row, then one line per site:

```csv
site_tag,base_url,output_dir,extra_seeds
```

| Column | Required? | What it means | Example |
|---|---|---|---|
| `site_tag` | **Yes** | Short uppercase-friendly tag identifying this site everywhere — filenames, folder names, the `runs`/`breakers` tables, `--config <tag>` later. Gets uppercased automatically. | `IEXINDIA` |
| `base_url` | **Yes** | The site's homepage. This becomes the crawl's first seed and is also used to derive `allowed_domains` (both the www and non-www variant are added automatically). | `https://www.iexindia.com` |
| `output_dir` | No | Where downloaded files, the site's workbook, and its checkpoint file get written. Leave blank and it defaults to `./downloads/<SITE_TAG>/`. | `./downloads/IEXINDIA` or blank |
| `extra_seeds` | No | Extra starting pages beyond the homepage, **semicolon-separated** if more than one. Useful for a known deep archive page that the auto nav/sitemap scan might not reach in one hop. Leave blank if you don't have any yet — auto-seeding from the nav menu and sitemap.xml still runs regardless. | `https://x.gov.in/tenders;https://x.gov.in/rti` |

A filled-in example for the five sites from this project's testing (fill
in the actual URL you used for powerexindia — left blank here rather than
guessed):

```csv
site_tag,base_url,output_dir,extra_seeds
ERLDC,https://erldc.in,,
APERC,https://aperc.gov.in,,
IEXINDIA,https://www.iexindia.com,,
POWEREXINDIA,,,
WRLDC,https://wrldc.in,,
```

**Rules worth knowing:**
- A row missing `site_tag` or `base_url` is silently skipped (not an
  error) — check the "Created N config(s)" count printed after running
  `--batch-add` matches how many rows you expected.
- If `sites/<tag>.json` already exists, that row is skipped and the
  existing config is left untouched — re-running `--batch-add` on the same
  CSV is always safe, it never overwrites.
- Every site created this way starts with **no fetch tier saved** — each
  gets freshly detected the first time it actually runs, same as a site
  added via `--init-config` (see the "never written back" note in
  `crawler.py`'s module docstring for why).
- `sites_to_add_TEMPLATE.csv` in this folder is a ready-to-copy starting
  point with this exact header row already in place.

### Running a saved batch of sites

The CSV above is for *creating* configs. Once several exist in `sites/`,
`--config all` runs literally every one of them — which includes any
older, unrelated config still sitting there from past testing, not just
the ones you meant to run today. For a specific, reusable subset, save a
plain text file with one site name per line:

```
# regulators.txt
aerc
aperc
apserc
berc
```

Blank lines and `#` comments are ignored. Then:

```bash
python3 crawler.py --config-file regulators.txt --mode inventory
```

Runs exactly those 4 — nothing else in `sites/` gets swept in. Keep a
small library of named batch files (`regulators.txt`,
`power_exchanges.txt`, `batch1.txt`) for anything you'll run more than
once; edit a line to add or remove a site rather than retyping a list.

For a genuine one-off you won't save, `--config aerc,aperc,berc` (comma-
separated, no file needed) does the same thing.

## Useful flags

| Flag | Default | What it does |
|---|---|---|
| `--config NAME\|NAME,NAME,...\|all` | — | Which `sites/<n>.json` to use, a comma-separated subset (e.g. `aerc,aperc,berc` — runs exactly those, unlike `all`), or `all` |
| `--config-file PATH` | — | Path to a text file, one site name per line — a saved, reusable batch (e.g. `regulators.txt`). Blank lines and `#` comments are ignored. Prefer this over the comma-list for anything you'll run more than once. |
| `--mode inventory\|download\|download-only` | `inventory` | Discover-only, discover-and-download, or download-only (skips the crawl, downloads whatever's already known from a previous run) |
| `--max-pages N` | 5000 | Hard cap on pages visited per site per run |
| `--max-depth N` | 10 | Max hops from a seed page |
| `--min-year YYYY` | 2020 | Skip documents dated before this year |
| `--respect-robots` | off | Opt IN to actually enforcing robots.txt for this run (checked/logged either way, but not enforced by default — see changelog) |
| `--ignore-robots` | (default already) | Kept for explicitness/backward compatibility; robots.txt isn't enforced by default anyway |
| `--parallel-sites N` | 5 | Max sites crawled concurrently with `--config all` |
| `--db PATH` | `crawler.db` | Shared database location |
| `--yes` | off | (reserved for future unattended-run confirmation prompts) |

## A platform note

If a site turns out to need tier 3 (full headed/visible Chromium — so far
only confirmed necessary for powermin.gov.in), that requires an actual
display attached to the machine, or a virtual one (`Xvfb`) — it will not
work over a bare SSH session with no GUI. Tiers 1 and 2 (plain requests,
stealth-headless) both run headless and work fine on any server.

## Changelog

Kept up to date with every real change made to this codebase — added to
going forward, not just written once.

### Merged contribution: SERC/CERC naming scheme + failure visibility

A second set of changes — the dual naming-scheme system (see "Naming
convention" above), the `NOT ABLE TO DOWNLOAD` console callout, red-row
highlighting in the workbook, and the `naming_scheme` database column —
was developed independently and merged in alongside everything below.
Verified line-by-line to confirm neither side's work was altered by the
merge; every fix below was layered on top of, not instead of, that work.

### Policy change

- **robots.txt is no longer enforced by default.** It's still fetched and
  checked every run, and every disallowed URL is still logged to
  `BREAKERS_LOG.xlsx` for awareness — but a disallowed URL is now fetched
  anyway rather than skipped, unless `--respect-robots` is explicitly
  passed (or `respect_robots: true` is set in a site's JSON config). This
  reversed the original default (respect robots.txt unless told not to)
  after a real run against niti.gov.in showed its robots.txt disallows
  effectively everything, which silently produced a 0-page crawl with no
  visible explanation. The old `multisite_crawler` never checked
  robots.txt at all, which is why sites that work fine under it can hit
  this the first time they're run under this codebase.

### Configuration changes

- Global defaults changed: `max_total_pages` 500 → **5000**, `max_depth`
  6 → **10**. Only affects sites created with `--init-config`/
  `--batch-add` AFTER this change — existing `sites/*.json` files keep
  whatever values were baked in when they were created; edit those
  files directly (or delete and re-`--init-config`) to apply the new
  defaults retroactively.

- **Leaf-URL protection was suppressing genuine pagination, not just
  dangerous filter combinations.** The leaf check (2+ query params on a
  page = don't follow its links, to stop combinatorial dashboard
  explosions) ran BEFORE the pagination check — so a listing page that
  combined a category filter with a page number (e.g.
  `?type=report&page=3`) had its own "next page" link silently dropped
  along with everything else, even though that link was completely
  legitimate. Found from a real gap: an older tool found ~3000 documents
  on niti.gov.in, this one found ~1750 on the same site. Fixed:
  pagination is now checked first, so a genuine next-page link survives
  even on an otherwise-leaf page; a real filter-combination link (not
  pagination-shaped) is still suppressed exactly as before. Every
  suppressed link is now also logged to `BREAKERS_LOG.xlsx` (signal:
  `leaf-url triggered`) instead of vanishing with no trace, so this kind
  of gap is visible going forward instead of only discoverable by
  comparing total counts against a different tool.

### Real bugs found from actual runs (most recent first)

- **powermin.gov.in's homepage passed every existing check while being
  functionally empty.** A live fetch returned barely more than a
  `<meta viewport>` tag — right status code, right content-type, long
  enough in raw bytes to clear the minimum-length check — but zero real
  links. The crawl finished after 1 page with 0 documents and no visible
  error, since nothing about the response looked like a failure. Fixed:
  `require_content` — a page must also contain at least 1 real link to
  count as successful, checked **only** for the initial homepage probe
  (`gather_auto_seeds`), not applied to every page in a crawl. The first
  threshold tried (2 links) immediately broke a real, previously-passing
  test — a legitimate single-document page has exactly 1 link and was
  being misclassified as a broken JS-shell — tightened to 1, matching the
  actual observed failure (zero links, not "fewer than some number").
- **powermin's config had stale seeds from before the site was
  redesigned** (confirmed: encoded content IDs, "persona" pages, and
  headless-CMS patterns all point to a recent rebuild) — every hand-
  picked seed 404'd. Rewritten using real paths confirmed directly from a
  live crawl, not trusted from an older reference config. Also fixed a
  literal Markdown-link string (`[text](url)`) that had been pasted
  directly into `allowed_domains` by mistake.
- **Two new opt-in per-site config fields, both built for powermin
  specifically and defaulting to off everywhere else:**
  `domain_blind_documents` (a document is recorded regardless of which
  domain hosts the file — powermin's real content lives partly on
  `rdss.powermin.gov.in`, `samarth.powermin.gov.in`, and a shared
  government asset server; page-*following* stays exactly as domain-
  restricted as ever) and `use_fixed_js_wait` (bypasses the adaptive
  JS-settle logic entirely for a site, waiting an unconditional 6 seconds
  on every page — matches an older tool's slower-but-proven approach,
  for a site whose real content loads asynchronously in a way the
  adaptive wait can cut off early). Combined with the seed correction
  above, powermin went from 18 total documents to 191 in the same run,
  confirmed from real console output both times.
- **A stale fallback default that was never updated when the robots.txt
  policy flipped.** `load_site_config`'s fallback for a config file
  missing the `respect_robots` key entirely still said `True`, left over
  from before the global default changed to `False`. Caught during a
  routine re-check of an unrelated feature, not from a report — no known
  config was actually affected, since every config in use was created
  after the field already existed.
- **CERC investigated directly** (by fetching the site's real pages, not
  guessing): most actual petition case documents — the petitions
  themselves, replies, rejoinders — live behind a separate, login-gated
  e-filing portal (`cerc-efiling.gov.in`). This is a hard, permanent
  limitation for any crawler without real credentials, not a bug.
  However, the *outcomes* of those cases (Orders, Records of Proceedings)
  are public on `cercind.gov.in` itself, organized through confirmed hub
  pages — `orders.html` (indexes back to 1999), `record_proceeding.html`,
  `recent_orders_rops.html`, `Current_Petition_diposed.html` — not yet
  added to `cerc.json`'s seeds.
- **A real architectural gap found on CERC, confirmed against another
  tool's actual source code:** this crawler locks an entire site to
  whichever fetch tier first succeeds and never re-tests a cheaper tier
  again for the rest of that run. A comparison tool re-tries plain
  `requests` fresh on every single page, independent of what happened on
  any other page. CERC spans roughly 20 years of infrastructure —
  thousands of simple static pages from 2008 alongside a modern JS-driven
  front end — so once its crawl escalates to the hardest tier (confirmed
  it does), every one of those simple old pages also gets forced through
  a real browser for the rest of the run. Proposed as an opt-in
  `per_page_tier_check` field, matching the same "opt-in, not global"
  pattern as everything else in this list — **not yet built**.
- **A silent skip path found on powermin, still unfixed.** A URL already
  in `visited` is dropped from the queue with zero console output —
  currently the only skip reason in the entire crawl loop that prints
  nothing. On a site whose nav menu likely repeats across every page,
  this plausibly means a large share of a reported "N queued" figure is
  duplicate noise draining invisibly at the end of a run, not genuine
  unexplored pages. Likely the actual explanation for why a run can look
  like it "stopped early" with pages still queued, when in fact the loop
  finished normally — **diagnosed, not yet fixed**.

- **`--config all` swept in every config in `sites/`, not just the ones
  someone actually meant to test.** Testing a fresh batch of 4 new sites
  with `--config all` also silently ran every older config still sitting
  in the folder from past testing (in one real case, an unrelated
  `iexindia.json` got run alongside a batch of 4 regulator sites). Fixed
  two ways: `--config-file <path>.txt` reads a saved, reusable batch (one
  site name per line, `#` comments allowed) — the preferred option for
  any subset you'll run more than once; `--config` also accepts a
  comma-separated list (e.g. `--config aerc,aperc,berc`) for a genuine
  one-off. Both resolve and validate every name *before* any site runs —
  a typo names the specific bad site and stops immediately, rather than a
  partial run or a silent skip — then hand off to the same concurrent-
  execution and reporting path `--config all` already uses. `--config
  all` and a single `--config <site>` are both unaffected.

- **A single scraped document title crashed an entire Excel export.**
  Excel's file format outright rejects certain control characters — one
  CERC document title contained one, and since a workbook is only saved
  once fully built, the crash happened mid-build and **nothing got
  saved at all** — not just that one row, all three export types for that
  run (workbook, breakers log, master summary), even though the
  underlying 21,000+ downloaded documents were completely safe in the
  database the whole time. Fixed: every value written to any workbook is
  now sanitized first (`_sanitize_for_excel`/`_sanitize_row` in
  `storage.py`), stripping only the illegal characters — normal unicode
  (em dashes, curly quotes, non-English text) is untouched.
- **`--config all` could lose or corrupt another site's row in the master
  summary.** Concurrent sites writing to the same `MASTER_SUMMARY.xlsx`
  with no locking is a classic lost-update race — confirmed by
  deliberately reproducing it: an unlocked 20-site concurrent test lost
  18-19 of 20 rows and in several runs corrupted the file outright
  (`BadZipFile`, `Truncated file header`). Fixed with a lock around the
  read-modify-write cycle; the same stress test then kept 20/20 every
  time.
- **Documents differing only by URL casing were treated as different
  files.** `Report.pdf` and `report.pdf` produced different database
  identities even though many government sites (especially IIS/Windows-
  hosted ones) serve files case-insensitively — meaning the same file
  could be downloaded twice under two names. Fixed: the URL *path* is now
  lowercased for identity matching (the query string's case is
  deliberately left untouched, since it can be meaningful there).
- **Permanently broken downloads were retried forever.** Every future
  `--mode download`/`download-only` run re-attempted every still-pending
  document indefinitely, with no way to tell a genuine 404 apart from one
  that just hadn't been tried yet. Fixed: a `download_attempts` column
  (added via the existing `_ensure_column` migration helper, so it
  reaches an existing `crawler.db` with no manual steps) caps retries at
  5 — the workbook distinguishes "still retrying" from "gave up after 5
  attempts."
- **PPT/PPTX/ZIP/RAR/TXT/RTF/ODT/ODS/ODP were not recognized as
  documents at all.** A real CERC reference count showed meaningful
  content served as `.ppt`/`.zip` specifically (bundled multi-file
  regulatory submissions); worse, `.ppt`/`.pptx` links were being queued
  as ordinary pages, which triggered a real "Download is starting"
  browser error and burned a full, permanent tier escalation to the
  slowest fetch tier for the rest of that run. Fixed: all of the above
  moved into `FILE_EXTENSIONS`; `.zip`/`.rar` removed from
  `SKIP_EXTENSIONS` accordingly (they're real documents now, not assets
  to ignore).
- **Windows path-length limit was an unaddressed risk.** Windows silently
  refuses to create a file path over ~260 characters unless given the
  special extended-length `\\?\` form — a real possibility given the SERC
  folder pattern stacks extra nesting on top of an already-long generated
  filename inside a deep project folder. Added `_windows_long_path()`,
  applied only at the moment of actually writing to disk (the clean path
  is still what's stored in the database and shown in Excel). Caught and
  fixed a real bug in this fix itself before it shipped: the call was
  initially placed *outside* the per-document `try/except`, meaning any
  failure in it would have crashed the entire download run instead of
  just that one document — moved inside the try block and verified with a
  forced-failure test that it now degrades to an ordinary per-document
  failure. **Honest limitation:** the string logic is fully tested, but
  an actual `\\?\`-prefixed write has not been exercised on a real
  Windows filesystem — first genuine confirmation will be the first time
  it actually triggers on a real run.

### Earlier bugs, from running this against iexindia.com and niti.gov.in

- **robots.txt blocks were completely silent.** A URL disallowed by
  robots.txt was logged to the breakers database but never printed to
  console — the only skip reason in the entire crawl loop that produced
  zero visible output. On a real run against niti.gov.in, this meant
  literally every one of 98 queued seed pages was silently blocked, and
  the run just ended with "0 pages visited" and no explanation at all —
  looked exactly like the crawler had silently failed for no reason.
  Fixed: every robots-blocked URL now prints `[robots-blocked] <url>` as
  it happens, and if any were blocked, a summary warning explains what
  likely happened (the site's robots.txt probably disallows everything
  under the crawled paths) and points at `--ignore-robots` as the
  deliberate override.
- **A `--base-url` with no `http://`/`https://` prefix failed identically
  at every fetch tier**, producing a confusing multi-tier failure trail
  for what was really just a missing prefix (e.g. `www.niti.gov.in`
  instead of `https://www.niti.gov.in`). Fixed: `_normalize_base_url()`
  now auto-prepends `https://` when it's missing, and rejects genuinely
  invalid input (no real domain, e.g. random text) immediately at
  `--init-config`/`--batch-add` time with a clear error, instead of
  letting it fail three tiers deep during an actual crawl. Applied to
  both the main `--base-url` and every `extra_seeds` entry.
- **Documents on a shared download endpoint were being silently merged.**
  `normalize_url_identity()` dropped the query string entirely when
  deciding if two URLs were "the same document" — meaning
  `/download?id=111` and `/download?id=222` collapsed to the identical
  identity and the second one was silently treated as a duplicate of the
  first, never even recorded as its own document. This was the real cause
  of documents appearing to be "already known" on what the person running
  it insisted (correctly) was a genuinely first, from-empty-database run.
  Fixed: the query string is now preserved; only scheme (`http`/`https`)
  and `www` are normalized away, since those really are the same
  resource.
- **Images/CSS/JS were being queued as "pages."** Their Content-Type isn't
  HTML, so the tier-1 fetch treated them as a failure requiring
  escalation — wastefully trying to bootstrap a full headed browser just
  to fetch a logo file. Fixed with an explicit skip-list checked before
  anything is queued.
- **Crash escalating from tier 2 to tier 3** ("Playwright Sync API inside
  the asyncio loop"). Tier 2's browser was still open when tier 3 tried to
  start its own — two live Playwright driver connections in one thread
  isn't supported. Fixed by closing a tier's browser before the next one
  opens.
- **"Download is starting" crash.** The bootstrap functions had no
  `try/except` around `page.goto()` (the per-page fetch functions did,
  bootstrap didn't) — any URL that made the browser try to start a file
  download instead of loading a page crashed the entire run. Fixed with
  the same try/except pattern already used elsewhere.
- **Classification taxonomy mismatch.** multisite's fixed ministry-style
  taxonomy (TENDER/RTI/GAZETTE/SCHEME/...) doesn't fit non-ministry sites
  — a power exchange has Career/Investors/Membership/Market-Data sections
  with no equivalent in that list, so everything fell through to generic
  MISC/GENERAL. Fixed by adding a second tier: if no keyword matches,
  derive category from the referring page's own URL path instead of
  defaulting to MISC. Generic seed hint is now only the last resort.
- **Auth-gated documents gave a cryptic raw error.** A document behind a
  real login (e.g. an investor-relations portal on a separate subdomain)
  redirects to a sign-in page, which returned a plain
  `403 Client Error: Forbidden for url: .../signin` — technically correct
  but didn't explain what actually happened. Fixed: a redirect to a
  sign-in/login/auth page now produces a clear message explaining the
  document is behind a login wall, not a broken URL. (The crawler does
  not attempt to bypass real authentication — if you have legitimate
  credentials for a portal like this, that would need dedicated login
  support added as its own feature, not a workaround.)
- **Threading crash on concurrent downloads.** Worker threads were
  sharing one SQLite connection object — not allowed. Fixed: every
  download worker opens its own connection; `storage.py` also got a
  `busy_timeout` so concurrent connections wait for locks instead of
  erroring.

- **`_MASTER_REPORTS/MASTER_SUMMARY.xlsx`** — a single, persistent
  master-rollup file that refreshes after ANY run, single-site or
  `--config all` (previously the master rollup only got created inside a
  `--config all` run, and even then wrote a brand-new timestamped file
  every time instead of updating one you could keep checking). Running
  `--config niti` alone now updates NITI's row in this file in place,
  leaving every other site's most recent numbers untouched. The old
  timestamped snapshots (`master_summary_<timestamp>.xlsx`, only written
  by `--config all`) still happen too, as a history — this is in addition
  to those, not a replacement.

### New capabilities added

- Every `[downloaded]`/`[failed]` line now shows elapsed time since the run
  started (e.g. `[downloaded] (12.3 min elapsed) ...`), continuously
  during a run, not just a total printed once at the end.
- `--mode download-only` — skips the crawl entirely and downloads
  whatever documents are already known in the database from a previous
  inventory/download run. Much faster than `--mode download` when you
  already trust an existing inventory and just want the files, at the
  cost of not picking up anything new that might have appeared on the
  site since that inventory was taken.

- Live console progress during a crawl — per-page line, `[tier
  escalation]` notices, checkpoint-save notices, a site header/footer,
  per-file download progress. Originally there was no output at all
  during a run.
- `--fresh` flag — deletes a stale checkpoint before crawling. Use this
  any time `crawler.py` itself gets updated, since an old checkpoint can
  carry forward queue entries a newer version would never have created.
- `--max-depth` flag, for CLI parity with `--max-pages` (previously only
  editable by hand-editing the site's JSON config).
- A `TOTAL DOCUMENTS FOUND` summary line with a file-type breakdown
  (PDF/XLS/etc.) at the end of every run.
- Cumulative download-status reporting (`get_download_status_counts()`) —
  a run's own console summary now shows the TRUE total downloaded/
  failed/pending across every run ever done for a site, not just the
  narrow slice of documents this particular run happened to attempt
  (previously misleading when most documents were already downloaded
  earlier and only a few stragglers remained).

### Reporting/wording fixes (no behavior change, clarity only)

- `documents_found` → `total_documents_known` / `documents_new_this_run`
  in the JSON summary — the old key was numerically identical to "new
  this run" despite reading like a running total.
- `downloaded`/`failed` in the summary → `downloaded_this_run`/
  `failed_this_run`, with new `total_downloaded`/`total_failed`/
  `total_pending` fields added for the real cumulative picture.
- End-of-crawl message clarified that an "already known" document can
  mean the same file found again via a different link within the SAME
  run (e.g. one PDF linked from several different pages), not only a
  separate prior run.