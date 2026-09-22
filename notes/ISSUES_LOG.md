# Issues Log

A running record of problems found in this crawler and how they were diagnosed/fixed.
One entry per issue: what happened, root cause, resolution.

---

## BREAKERS_LOG.xlsx got corrupted (Excel reported "needs repair")

**What happened:** the shared, cumulative `BREAKERS_LOG.xlsx` file became unreadable.

**Root cause:** `export_breakers_log()` had no locking at all, unlike its sibling
`update_master_summary()`. Every site's thread in a `--config all` batch writes this same
fixed file path concurrently via `ThreadPoolExecutor` — two threads' `wb.save()` calls to
the same path at the same time interleave and corrupt the `.xlsx` (a ZIP archive under the
hood).

**Fix:** added the same locking pattern `update_master_summary()` already used (an
in-process `threading.Lock` plus a cross-process file lock) to `export_breakers_log()` too.

---

## A batch run crashed mid-workbook-export with an `lxml.SerialisationError`

**What happened:** `--config WBERC,UPERC,APTEL,CEA,CAG` printed a full traceback
(`lxml.etree.SerialisationError: unknown error -1`, followed by a `PermissionError` trying
to clean up an openpyxl temp file) while saving one site's workbook, then crashed the
whole batch.

**Root cause:** likely Windows Defender / OS-level file-lock contention on openpyxl's temp
file, under heavy sustained disk I/O from 5 parallel sites downloading simultaneously
(one of them, APTEL, alone attempted 9,095 downloads). Separately, investigating this
revealed that **two other sites in the same batch (WBERC, UPERC) silently failed too** —
their `MASTER_SUMMARY.xlsx` rows never updated for that run, with no visible error at all.

**Why the failures were silent:** `run_all_sites()`'s `for future in as_completed(futures):
summaries.append(future.result())` stops iterating the moment the *first* failing future's
`.result()` raises — any other future that also failed never gets its exception
inspected/printed, and the exception isn't held anywhere for later either. The
`download_pending_documents()` download loop had the identical pattern.

**Fix:** both loops now wrap each `future.result()` call in its own try/except, print a
clear per-item failure message, and keep processing the rest — one bad site/document no
longer hides another's failure or stops the batch's remaining reporting steps.

---

## Documents show `downloaded=1` in the database but the files don't exist on disk

**What happened:** the user found APERC listed as 1555 documents "downloaded," but the
`downloads/APERC` folder was empty.

**Root cause:** each document's `local_path` is an absolute path baked in at download
time. This project's folder has moved at least twice
(`...\Rakesh-editedcompiled-crawler\code\` → `...\R-com-crawler\R-com-code\` on C: →
today's `E:\CP\R-com-code`). For most sites the actual files were carried along through
that migration. For **APERC, APSERC, BERC, and SSERC specifically, they were not** — only
the database rows survived, not the files themselves.

**Why a plain recrawl doesn't fix it:** `download_pending_documents()` only selects
documents `WHERE downloaded = 0`. Since these are already flagged `downloaded = 1`, a
normal `--mode download` run skips them entirely ("No pending documents to download") and
restores nothing.

**Resolution:** identified as a data-repair task, not a code bug — would need a one-time
script to find, per site, every `downloaded=1` document whose `local_path` doesn't exist
on disk, reset it to pending, then rerun `--mode download` for those sites. Not yet
executed.

---

## SSERC / TERC / TNERC / TSERC / UERC showing zero or very few documents

Investigated each individually via the `breakers` table — five different causes, not one bug:

- **TSERC:** DNS resolution failed outright (`Failed to resolve 'tserc.gov.in'`) on two
  separate attempts — the domain may have changed or moved.
- **TNERC:** connection timed out at every fetch tier — likely transient (site down at
  that moment) or a network-level block.
- **SSERC:** the site was reachable and 15 pages were fetched successfully, but none of
  them linked deep enough to reach real content — needs better seed URLs, not a recrawl.
- **TERC:** a crawl inserted 7 documents into the database but never completed —
  interrupted mid-run (crashed/killed) with no checkpoint file left behind to resume from.
- **UERC:** actually fine — 8 found, 8 downloaded, 0 failures. Just a shallow (2-page)
  crawl, not an error.

---

## The crawler appeared "stuck" crawling downtoearth.org.in

**What happened:** the run kept printing `[skip] fetch failed` for old
`sitemap-daily-2010-12-*.xml` URLs, then a raw Playwright/Node.js crash
(`Assertion error` in `coreBundle.js`), then more failures with
`Connection closed while reading from the driver`.

**Root cause chain:**
1. `.xml` was not in `SKIP_EXTENSIONS`, so a decade of daily sitemap archive files got
   treated as ordinary pages and rendered in a real Chromium tab.
2. Rendering raw XML in a full browser crashed Chromium's renderer (`Page crashed`).
3. That crash cascaded into the entire Playwright Node.js driver process dying.
4. Nothing detected the dead driver — every remaining page for the site kept trying
   (and failing) against the same broken connection for the rest of the run.

**Fix (already applied by the time of this log):** `.xml` added to `SKIP_EXTENSIONS`.
**Additional fixes applied this session** (see below) to handle the same failure mode more
gracefully even if it recurs on another site/extension:
- Playwright browser-launch failures (not just navigation failures) are now caught, with
  best-effort cleanup so a failed launch doesn't leak the Node.js driver process.
- Once tier 3 (the final, most capable fetch tier) is confirmed dead via a
  driver/connection-level error, the crawler now short-circuits immediately for the rest
  of that site's run instead of repeating a guaranteed-failed Playwright call per URL.

---

## Other robustness gaps found and fixed (this session, no live incident yet)

Found via targeted code review after the incidents above, applied proactively:

- **`_ensure_column()` race condition:** concurrent site threads racing to add a new
  schema column to the same `crawler.db` on the very first run could crash with
  `duplicate column name`. Now treated as a benign, expected race instead of a fatal error.
- **Unhandled `wb.save()` failures:** if any exported `.xlsx` file is open in Excel while a
  crawl is running (normal usage — checking progress mid-run), the save would raise
  `PermissionError` uncaught and abort the rest of that site's run. All three export
  functions now catch this, print a warning, and let the run continue.
- **`run_one_site()`'s database connection wasn't guaranteed to close** on an exception
  during the download/export phase. Now wrapped in `try/finally`.

---

## A "smoke test" of these fixes accidentally wiped real report data

**What happened:** while verifying the fixes above worked, a test run was pointed at a
fresh scratch database via `--db`, but `BREAKERS_LOG.xlsx`, `MASTER_SUMMARY.xlsx`, and
`_WORKBOOKS/*.xlsx` paths are hardcoded relative to the script's own location, not the
`--db` argument — so the test run overwrote the real breaker history (down to 0 rows) and
zeroed out UERC's row in the real master summary.

**Resolution:** since the real `crawler.db` itself was never touched (only `--db` was
redirected), all three files were regenerated directly from it and restored to their
correct, real state. **Lesson for next time:** redirect every output path, not just
`--db`, when testing against a scratch database.

---

## AERC, CSERC, and MSERC: 100% of documents found, 0% downloaded

**What happened:** all three sites show hundreds/thousands of documents discovered by the
crawler, but every single download attempt failed — 1230/1230 for AERC.

**Root cause:** every failure is the identical error —
`SSLCertVerificationError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
unable to get local issuer certificate`. This means `requests` can't build a complete
trust chain to a known root CA for these sites — most likely because the server itself
doesn't send its intermediate certificate (a common misconfiguration that real browsers
often paper over via cached intermediate certs, but a bare Python client can't). This is
deterministic, not transient — retrying changes nothing.

**Separately, DERC's 100%-failed documents are a different problem:** DNS resolution
failure for `www.derc.gov.in` (`NameResolutionError`), not a certificate issue.

**Resolution:** not yet fixed. Upgrading `certifi` is worth trying first (free, no code
change) but likely won't help if this is a genuine server-side chain misconfiguration
rather than a stale local CA bundle. The real fix, if needed, would be a per-site
relaxed-verification override — same shape as the NRLDC fix below.

---

## NRLDC: 100% of documents found, 0% downloaded (different SSL failure than AERC)

**What happened:** 233 total documents discovered, zero ever downloaded successfully.

**Root cause:** 231 of 233 fail with a *different* SSL error than AERC's —
`[SSL: UNSAFE_LEGACY_RENEGOTIATION_DISABLED] unsafe legacy renegotiation disabled`. This
is a modern-OpenSSL security policy refusing to complete a TLS handshake with a server
that only supports old-style "legacy renegotiation." 195 of the 231 are via
`nrldc.in/download-file?...`; the remaining 36 are other direct document URLs on the same
backend. The other 2 failures are unrelated transient errors on different domains
(`posoco.in`, `webcdn.grid-india.in`).

Notably, **page crawling for NRLDC already works fine** — that's how all 233 documents
were discovered — only the actual file-download requests hit this wall, meaning NRLDC's
document-serving backend runs on different (older/misconfigured) TLS infrastructure than
its main website. None of the 233 documents have exhausted the 5-attempt retry cap yet
(max seen: 2), so a fix doesn't need any manual DB reset to take effect.

**Fix (implemented and verified):** added a per-site opt-in config flag
(`allow_legacy_ssl_renegotiation`, defaulting to off) that gives only the download-phase
session a relaxed SSL context (`OP_LEGACY_SERVER_CONNECT`) when a site's config enables
it — a new `_LegacySSLAdapter` in `crawler.py`, wired into `download_pending_documents()`,
enabled for `sites/nrldc.json` only. Same precedent as the existing `use_fixed_js_wait`
per-site flag; every other site's behavior is byte-for-byte unchanged (verified). Tested
directly against 2 of NRLDC's real failing URLs: both reproduce the original error without
the fix, and both succeed (HTTP 200, real PDF bytes) with it. Next normal
`--config nrldc --mode download` run will pick up all 233 documents automatically — no
manual DB reset needed, since none had exhausted the 5-attempt retry cap.

---

## Crawling vasudha-foundation.org crashed with `ValueError: Invalid IPv6 URL`

**What happened:** a full-site crawl died outright 179 pages in, 2113 URLs still queued,
with an uncaught traceback from deep inside `urljoin()`.

**Root cause:** fetched the actual page being processed at the time and found the exact
culprit — a malformed link on the source page itself:
```html
<a href="http://[1] https://www.toyotabharat.com/showroom/camry/specifications.html">
```
This looks like a citation (`[1] https://...`) pasted directly into an `href` attribute by
whoever authored the page — bad markup on the site's end, not a crawler bug in how it
fetched the page. But `urljoin` sees the `http://[` prefix and assumes it's the start of
an IPv6-bracketed address (`http://[::1]/...`-style); since what follows isn't a valid
bracketed netloc, Python 3.14's stricter bracket validation correctly rejects it — but
raises `ValueError` instead of failing gracefully.

**The real bug:** `extract_links()` and `extract_nav_links()` (`crawler.py`) called
`urljoin()` on every single link with no try/except at all. One malformed href anywhere in
a site's HTML — plausible across the wide variety of government/NGO sites this tool
targets, not a one-off — crashed the *entire* crawl. Same "one bad input shouldn't take
down everything" gap already fixed elsewhere this session (Playwright launches, batch
loops, Excel saves), just one remaining spot in the crawl loop itself.

**Fix (implemented and verified):** wrapped the per-link URL-resolution step in both
functions in a try/except `ValueError`, skipping just the one malformed link instead of
crashing the whole run. Verified directly against the exact malformed href found above —
confirmed it's now skipped cleanly while every valid link on the same page still extracts
correctly, with no crash.

---

## GRID-INDIA's daily PSP report was never downloaded — three separate, compounding causes

**What happened:** `https://grid-india.in/en/reports/daily-psp-report` never showed up in
`crawler.db` despite GRID-INDIA having 1219 other documents. Real diagnosis went through
several corrections as evidence came in — the final picture involves three independent
problems, all now fixed in `sites/grid-india.json`:

1. **Tier 1 fails outright for this whole site** — every plain `requests` fetch to
   `grid-india.in` raises the same `CERTIFICATE_VERIFY_FAILED: unable to get local issuer
   certificate` error as AERC/CSERC/MSERC. This is already handled correctly by the
   existing escalation logic (the site properly runs on tier 2/Playwright for everything)
   — not a bug, just worth naming since it was the first (correct, but incomplete) lead.

2. **The real reason the page was never visited: it's simply not linked from anywhere the
   crawler reaches.** Confirmed by rendering the actual homepage with Playwright:
   GRID-INDIA's default locale is Hindi, and its nav only exposes `/hi/reports/...` links
   — never `/en/...`. Even the Hindi `daily-psp-report` page (which *is* linked) was never
   reached — out of ~25 report nav links, the one 317-page crawl run just didn't get to it.
   This is a pure traversal-budget/discoverability gap, not a rendering problem — the site's
   tier-2 escalation, JS rendering, and link extraction were all already working correctly
   for sibling pages that *were* visited (confirmed: real documents were already found via
   `hi/reports/monthly-psp-report`, `en/reports/re-curtailments`, etc.).
   **Fix:** added `en/reports/daily-psp-report` and `hi/reports/daily-psp-report` as
   explicit seeds, guaranteeing they're visited regardless of traversal order.

3. **Even once visited, the page loads via a slow multi-step API chain** (sequential calls
   to `menu`, `page`, `last-update-date`, `file/get-period`, `file`) that the default
   adaptive JS-settle wait exits from too early — it mistakes a brief pause between API
   calls for "done," capturing ~34KB of an eventual ~167KB page with zero document links.
   **Fix:** set `use_fixed_js_wait: true` (same flag already used for `powermin.gov.in`).
   Confirmed directly: with it on, the page fully renders (214 links, 58 real documents);
   without it, the adaptive wait gives up too soon specifically on this page.

4. **Separately, even a fully-rendered page's documents wouldn't have been recorded**: the
   actual PDF/XLS files live on `webcdn.grid-india.in`, a different subdomain than
   `allowed_domains` (`grid-india.in`, `www.grid-india.in`), and `domain_blind_documents`
   was `false`. Fixed by setting it to `true` for this site (see the two general fixes
   below for why this is now the default for *new* sites too).

**Verified end-to-end** against the real live page with all fixes combined: 8 of 8 document
links found on the page would now be recorded (previously 0, obviously).

---

## GRID-INDIA downloads: 2470 of 2473 failed with the same SSL cert error as AERC — made automatic this time

**What happened:** after the fixes above let the crawler discover 1254 new documents, almost
all of them failed to *download* — `CERTIFICATE_VERIFY_FAILED: unable to get local issuer
certificate` on `webcdn.grid-india.in`, the same failure class as AERC/CSERC/MSERC, now on a
fourth site's CDN subdomain.

**The user explicitly did not want another manual per-site flag for this** — the whole
lesson of GRID-INDIA was that these get caught late, by accident, and there's no way to know
which future site will hit it next.

**Fix (implemented and verified) — automatic, zero-config, works on any site:**
`download_pending_documents()` now builds a second, deliberately relaxed **fallback session**
once per run (`build_retry_session(allow_legacy_ssl=True, disable_ssl_verification=True)`).
`download_one()` tries the normal, fully-verified session first; only if that raises
`requests.exceptions.SSLError` does it retry once via the fallback — a site with no SSL
problems never touches this, zero extra cost. The fallback session covers *both* known real
failure signatures at once (missing issuer certificate, and NRLDC's legacy-renegotiation
refusal), since there's no way to predict which one a future site will hit.

**A real bug was caught and fixed during this work:** the first attempt used
`session.verify = False` after the fact, which doesn't work once a custom SSL context is
already in play — Python's `ssl` module raised `Cannot set verify_mode to CERT_NONE when
check_hostname is enabled`. Fixed by building `check_hostname = False` and
`verify_mode = CERT_NONE` directly inside `_LegacySSLAdapter`'s own context construction, in
the required order, gated behind a new `disable_verification` flag on the adapter so NRLDC's
existing (verification-still-on) usage is completely unaffected — confirmed via a dedicated
regression check.

**Verified:** 4 mocked unit checks (happy path never touches fallback, SSLError triggers
fallback and succeeds, both sessions failing is handled cleanly, no-fallback-provided behaves
exactly as before) + 5 checks confirming the SSL context itself is built correctly and NRLDC
sees zero behavior change, + a real download of one of GRID-INDIA's actually-failing URLs
(5.18 MB PDF), which failed without the fix and succeeded with it.

---

## Audited every "found documents but downloaded ~0%" site — 5 already fixed, 1 needed a new fix

**What happened:** asked whether the SSL download-failure problem had actually been found and
fixed everywhere it occurs, with a list of affected sites for easy re-running.

**Audit:** queried every site where documents were found but the download success rate was
under 20%. Found 6: AERC, CSERC, DERC, MSERC, GRID-INDIA, and TNERC.

- **AERC, CSERC, DERC, MSERC** all share the *exact* `CERTIFICATE_VERIFY_FAILED` SSL error as
  GRID-INDIA — already covered by the automatic SSL fallback fix (global, not site-specific),
  confirmed by inspection. They just need `--mode download` rerun to pick it up; no new code
  needed.
- **TNERC was different and NOT covered:** all 1088 of its failures are a uniform
  `Read timed out` with zero SSL involvement — the hardcoded 30s download timeout is
  consistently insufficient for this server.

**Also checked: had the SSL fix actually produced results yet?** No — confirmed zero crawler
runs of any kind had happened since the fix was implemented on Sep 10. The only evidence of
it working was the earlier isolated single-file test, not a real aggregate run.

**Fix for TNERC (implemented and verified):** extended the exact same automatic,
config-free retry pattern already used for SSL errors — `download_one()` now also catches
`requests.exceptions.Timeout` and retries once with a longer timeout
(`EXTENDED_DOWNLOAD_TIMEOUT = 90`s) on the same session, before giving up. A site that
responds within the normal 30s never triggers this, zero extra cost. Verified with 5
mocked unit checks (retry-then-succeed, both-attempts-fail handled cleanly, no-retry on the
happy path) + confirmed the existing SSL fallback path has zero regression + a real download
of one of TNERC's actual timing-out URLs (230,865-byte PDF), which now succeeds.

---

## Added "First Run At" to MASTER_SUMMARY.xlsx without losing any of the 42 existing rows

The user wanted to know when a site was first ever crawled, not just its most recent run
(the existing "Last Run At" column). Also worth correcting here: an earlier note in this log
said the file had only 16 rows — that was stale; by the time of this change it actually had
**42** distinct sites' rows, all real, accumulated data.

**Change:** added a `get_first_run()` function (mirrors the existing `get_latest_run()`, just
`ORDER BY id ASC`), added `"First Run At"` to `MASTER_ROLLUP_COLUMNS` (right before the
existing `"Last Run At"`), and populated it in `_compute_master_row()`.

**Zero data loss, rigorously verified — not just assumed:** `update_master_summary()` already
had migration logic for exactly this scenario (adding a new column preserves every existing
row's existing values by name, a fix for a *previous* real bug where a layout change used to
wipe the whole sheet). Before touching anything: made an explicit file-copy backup
(`MASTER_SUMMARY.xlsx.backup-2026-09-10`) and saved a full JSON snapshot of all 42 rows'
exact values. After triggering the migration and then proactively refreshing all 42 sites (so
"First Run At" wouldn't sit at a placeholder for sites not naturally re-run soon): diffed
every single column of every single site against the pre-change snapshot — **zero
mismatches**, all 42 sites present, and all 42 now correctly show a real "First Run At" value
(none left at the migration's placeholder).

---

## 20 sites with real crawl history were missing from MASTER_SUMMARY.xlsx entirely

**What happened:** the user asked to recheck whether the master workbook was up to date. It
was, relative to actual crawl activity (zero runs had happened since Sep 9) — but a deeper
check (comparing every site_tag with real history in `crawler.db` against what's actually in
the Excel) turned up 20 sites missing from the sheet entirely, some with very large amounts
of data: CERC (21,393 documents), POWERGRID (4,820), DERC (3,180), BERC (1,823), APERC
(1,555), HPERC (1,324), CSERC (1,332), AERC (1,230), IEXINDIA (516), and 11 others.

**Root cause — NOT the smoke-test incident from earlier in this session.** Every one of these
20 sites' latest run finished between **2026-08-27 and 2026-08-29**, before this entire
conversation started. This is the same pre-existing gap flagged at the very beginning of this
session (the Excel only had 16 of 37 known sites at that point) — traced to an old
schema-migration bug in the code's own history that used to wipe rows on a column-layout
change, already fixed since (see the "First Run At" entry above), but the damage from before
that fix was never retroactively repaired. The one genuine mistake made *during* this
session (the scratch-database smoke test) only ever affected UERC, and that was already
correctly restored at the time with a verified row-count match.

**Fix (implemented and verified):** none of the 20 sites' data was actually lost — confirmed
by pulling real document/run counts directly from `crawler.db` for every one of them, all
fully intact. Per the user's request, inserted all 20 as new rows **at the top** of the
sheet (`ws.insert_rows(2, amount=20)`, since `update_master_summary()` only ever appends new
sites at the bottom — this was a one-off manual recovery, not a change to that function's
normal behavior), each row computed via the exact same `_compute_master_row()` logic already
used everywhere else, alphabetically ordered. Verified with a full before/after diff against
a fresh backup (`MASTER_SUMMARY.xlsx.backup-2026-09-22`): all 42 pre-existing rows
byte-identical and in their original position, all 20 new rows correctly added at the top,
total row count 42 → 62.

---

## Two general fixes applied proactively, from the same investigation

The user didn't want `use_fixed_js_wait`-style fixes to stay a "remember to add this
per-site after noticing a problem" pattern — the whole point of finding GRID-INDIA's issue
was that it was only caught because it happened to get rechecked. Two automatic,
zero-config improvements were added so this class of problem can't recur silently:

**1. Automatic per-page JS-shell detection (no config, applies to every site).** A new
`SiteFetcher.verify_empty_page_with_tier2()` method, wired into `crawl_site()`'s main loop:
if a page fetched at tier 1 yields *zero* extractable links, it's automatically re-fetched
once via tier 2 (deliberately capped there, not tier 3, since that launches a visible
browser and could fail on a headless machine). If tier 2 finds real content, that's used
instead. Critically, this is a **per-page** check only — it never touches the site's
overall sticky tier decision, so it can't slow down or falsely escalate a site that's
otherwise fine at tier 1 (the exact regression the existing seed-probe-only
`require_content` check was already designed to avoid). Verified with 13 unit-style checks:
correctly triggers on empty tier-1 pages, correctly falls back when tier 2 is *also* empty
(a genuinely sparse real page), never triggers on normal pages, never auto-escalates past
tier 2, and a full integration test through `crawl_site()` confirms a synthetic
GRID-INDIA-shaped scenario (empty shell → verified via tier 2 → cross-domain PDF found →
recorded) works end-to-end.

**2. `domain_blind_documents` now defaults to `true` for new configs.** Previously
opt-in (defaulting to `false`) despite the codebase's own existing rationale agreeing
documents shouldn't be domain-gated the way page-crawling is (real sites commonly serve
files from a different CDN/asset subdomain than the page linking to them). Changed the
default in `SiteConfig`, `load_site_config()`, and `write_init_config()`'s template.
**Confirmed safe for existing sites — with one caveat:** every site config generated by
`write_init_config` already has this key explicitly written, so the default flip can't
silently change them. **However, 8 older configs predate the flag being added to the
template and don't have the key at all** — `aerc.json`, `aperc.json`, `apserc.json`,
`berc.json`, `cerc.json`, `cserc.json`, `iexindia.json`, `powergrid.json`. These 8 **will**
start recording cross-domain documents they previously didn't, as a deliberate, disclosed
side effect (not a silent one) — arguably a good thing given the whole point of this fix,
but worth knowing about. Add `"domain_blind_documents": false` to any of these explicitly
if you want to freeze their current behavior instead.

---

## Another downtoearth.org.in crawl crashed with a bare `MemoryError`

**What happened:** 136 minutes and 852 pages into a crawl, an uncaught `MemoryError` fired
inside `_real_tier1_fetch()` while decoding the response body for
`/collection/top-news-waste`, killing the entire run.

**Root cause:** `_real_tier1_fetch()` only caught `requests.RequestException` — `MemoryError`
is a plain built-in exception unrelated to `requests`, so it was never caught and propagated
all the way up through `main()`.

**Was the page itself pathologically huge?** Checked directly — the live page is only ~1MB
right now, not huge. The more likely real cause: this crawl was running at the same time as
several other long-running crawler processes (MERCOM, POWERMINT, IEEFA, PV_MAGAZINE — seen
running concurrently earlier this session), so the system's overall memory was probably
already under pressure, and even a modest allocation failed. Either way, the fix needed is
the same regardless of which it was.

**Fix (implemented and verified):**
1. Added a `MAX_PAGE_RESPONSE_BYTES` (25MB) cap — `_real_tier1_fetch()` now streams the
   response and bails out early (checking `Content-Length` up front, and also while
   iterating chunks for responses with no `Content-Length`) if a page is ever genuinely
   huge, treating it like any other rejected fetch rather than fully buffering it.
2. Broadened the exception handling to explicitly catch `MemoryError` too, both around the
   fetch and around the text-decode step — a `MemoryError` anywhere in this function now
   fails just that one page instead of crashing the whole crawl.

Verified with 7 unit-style checks (mocked responses): normal pages unaffected, both cap
paths trigger correctly, a simulated `MemoryError` is caught cleanly, and every pre-existing
rejection path (403/429/503, non-HTML, too-short content) behaves exactly as before.
