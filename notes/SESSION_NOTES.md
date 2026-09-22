# Session Notes

A structured recap of this conversation with Claude about the R-com crawler codebase
(`e:\CP\R-com-code`), in the order topics came up.

## 1. Config basics — where new site output goes

Asked how `output_dir` is determined for a newly created site config. Answer: `--init-config`
requires `--output-dir` explicitly (no default); `--batch-add` via CSV defaults to
`<script_dir>/downloads/<SITE_TAG>` if the CSV's `output_dir` column is left blank.

## 2. Where the breakers log is written, and why it got corrupted

`BREAKERS_LOG.xlsx` lives next to `crawler.py` and is regenerated from scratch on every
run from the SQLite `breakers` table (cumulative across all sites/runs, since nothing is
ever deleted from that table). Diagnosed that it lacked the concurrency locking its sibling
`update_master_summary()` already had — see the Issues Log for the fix.

## 3. Analyzing crawler.db — run stats after 2026-08-28

Queried the `runs` table for the latest run per site since a given cutoff date. Explained
the difference between `documents_found`/`documents_new` (new discoveries this run),
`documents_known` (re-encountered duplicate links this run, not a cumulative total), and
`downloaded`/`failed` (this run's download attempts).

## 4. MERC / MERC-MSLDC "download count doesn't match what I see"

The user noticed only 20 (MERC) / 0 (MERC-MSLDC) documents downloaded in the *latest* run,
but thousands of files exist on disk. Clarified that each site had **two** runs — most of
the downloading happened in an earlier run, the later run just topped up a handful of new
documents. Verified the cumulative totals (documents table + on-disk file count + summed
`runs.downloaded_count`) all agreed: 2041 for MERC, 2176 for MERC-MSLDC.

## 5. "True site totals" for all sites

Built a full cumulative table (Total / Downloaded / Failed / Pending / Last Run) per site,
pulling from `crawler.db` directly rather than `MASTER_SUMMARY.xlsx` after discovering the
Excel file was missing many sites' rows entirely (12+ sites present in the DB weren't in
the spreadsheet — likely lost in an earlier schema-migration incident the code's own
comments warn about).

## 6. Why SSERC / TERC / TNERC / TSERC / UERC show zero or few documents

Investigated each via the `breakers` table — five distinct causes (DNS failure, connection
timeout, thin site navigation, an interrupted crawl with no checkpoint, and one that was
actually fine). See the Issues Log for the breakdown and per-site recrawl recommendation.

## 7. APERC shows 1555 documents downloaded but the folder is empty

Traced this to `local_path` values pointing at a defunct absolute path from a previous
project location (`...\Rakesh-editedcompiled-crawler\code\...`). Cross-checked every site
and found four total affected (APERC, APSERC, BERC, SSERC) where the files never survived
a past folder migration — only the database rows did. Explained why a plain recrawl can't
fix this (`download_pending_documents()` only touches `downloaded=0` rows).

## 8. Relative `output_dir` in a new config (JSERC) — which drive does it resolve to?

Explained that `load_site_config()` never makes a relative `output_dir` absolute — it
resolves against whatever the current working directory is when `python crawler.py` is
invoked, not against the script's own location. Recommended pinning it to an absolute path
to avoid the exact kind of drift found in item 7.

## 9. Analyzing the whole codebase for slow-loading-site handling (10s–2min pages)

Deep dive into every timeout constant (`REQUEST_TIMEOUT=20s`, `PAGE_TIMEOUT_MS=45s`), the
adaptive JS-settle mechanism (polls page content length every 300ms, hard-capped at a flat
6 seconds regardless of `use_fixed_js_wait`), and the one-way tier escalation ladder.
Conclusion: a site needing 45s–2min to load reliably fails or silently captures incomplete
HTML, since neither the navigation timeout nor the JS-settle ceiling has any per-site
override in the current config schema.

## 10. Applying that analysis to a real site: nerldc.in

Attempted to fetch the site directly to get empirical timing data; it failed DNS
resolution from this environment (flagged as possibly sandbox-specific, worth the user
checking independently). Walked through both slowness scenarios (slow server response vs.
slow client-side JS rendering) and what each would look like against this crawler's fixed
timeouts.

## 11. A batch run got stuck crawling downtoearth.org.in

Diagnosed the full chain: `.xml` sitemap files weren't excluded from normal page-crawling,
one got rendered in a real browser tab and crashed Chromium, that crash killed the entire
Playwright driver process, and every subsequent page for that site then failed against the
dead connection for the rest of the run. See the Issues Log for the fix chain.

## 12. Pushing the code to GitHub

Found `gh` CLI wasn't installed. Wrote a `.gitignore` excluding runtime data
(`crawler.db*`), `downloads/`, generated reports, and — per the user's explicit choice —
`sites/` (contains machine-specific absolute paths). Provided the manual `git`
command sequence since the user wanted to run it themselves rather than have `gh`
installed.

## 13. Pushing Claude's own memory files too?

Clarified that Claude Code's per-project memory lives entirely outside the repo
(`C:\Users\...\​.claude\projects\...\memory\`), that nothing existed there yet for this
project, and that pushing it to GitHub wouldn't make it usable elsewhere automatically —
Claude only reads memory from that exact local path.

## 14. Making the codebase more robust + this documentation

Asked what else, beyond the already-applied `.xml` fix, should be fixed for robustness.
Two research passes turned up six further confirmed gaps (breakers-log locking, two
`as_completed` loops silently swallowing all-but-the-first failure, uncaught Playwright
launch failures leaking the driver process, no short-circuit once the final fetch tier is
confirmed dead, uncaught `wb.save()` failures, and a schema-migration race condition). All
six were implemented and verified — see the Issues Log for details, including an
accidental data-wipe during smoke-testing that was caught and fully repaired from the real
database. This file and `ISSUES_LOG.md` were written at the same time, per the user's
request for a local record of this session and its problems/fixes.

## 15. Full re-verification after the 7 fixes

Asked to confirm nothing else was broken. Built an isolated test suite (25 targeted checks
exercising each fix's exact exception branches, using mocks constrained to match the real
Playwright API's actual attribute surface) plus a real, non-mocked end-to-end run against
live sites (UERC, RERC) with every output path redirected to scratch this time — all 34
checks passed. Caught and fixed one mistake along the way: an earlier smoke test had only
redirected `--db`, not the hardcoded report paths, silently wiping real data in
`BREAKERS_LOG.xlsx` and `MASTER_SUMMARY.xlsx` — restored from the untouched real database.

## 16. NRLDC and AERC/CSERC/MSERC: two different SSL failure modes, both 100% of downloads

Diagnosed why NRLDC (233 documents) and AERC (1230), CSERC, and MSERC each show 0%
download success despite successful crawling. Two distinct, unrelated TLS problems:
AERC/CSERC/MSERC hit `CERTIFICATE_VERIFY_FAILED` (can't build a trust chain — likely a
missing intermediate cert server-side); NRLDC hits `UNSAFE_LEGACY_RENEGOTIATION_DISABLED`
(a stricter modern-OpenSSL policy the server's old TLS stack can't satisfy). Also
evaluated and ruled out a separate, real-but-unrelated architectural gap (the crawl-time
session's cookies never get copied into the download-time session) as the cause — TLS
handshake failures happen before cookies are ever relevant. Scoped a fix for NRLDC (a
per-site `allow_legacy_ssl_renegotiation` flag, download-session-only) and logged both
issues in `ISSUES_LOG.md`. The user then asked to implement it after all — built and
verified in the same session: a new `_LegacySSLAdapter`, wired into
`download_pending_documents()`, enabled only for NRLDC. Confirmed with a real network
test against 2 of NRLDC's actual failing URLs (fails without the fix, succeeds with it —
HTTP 200, real PDF bytes) and unit-style checks confirming every other site's behavior is
completely unchanged.

## 17. A crawl of vasudha-foundation.org crashed with `ValueError: Invalid IPv6 URL`

Fetched the actual live page that crashed the crawl and found the exact cause: a malformed
`href` on the source page itself (a citation like `[1] https://...` pasted directly into
an href), which `urljoin` misreads as an invalid IPv6-bracketed address and raises on
rather than failing gracefully. The real bug was that `extract_links()`/
`extract_nav_links()` had no try/except around their `urljoin()` calls at all, so one bad
link anywhere on a page crashed the whole crawl — the same "one bad input shouldn't take
down everything" class of gap already fixed elsewhere this session, just one remaining
spot. Fixed and verified directly against the exact malformed href: it's now skipped
cleanly, valid links on the same page are unaffected. Logged in `ISSUES_LOG.md`.

## 18. Another downtoearth.org.in crawl crashed, this time with a bare `MemoryError`

136 minutes and 852 pages into a crawl, decoding one page's response body raised an
uncaught `MemoryError` — not caught by `_real_tier1_fetch()`'s narrow
`except requests.RequestException`, so it killed the whole run. Fetched the actual live
page directly to check whether it was pathologically huge: it wasn't (~1MB) — more likely
this crawl was competing for memory with several other long-running crawler processes
running at the same time (MERCOM, POWERMINT, IEEFA, PV_MAGAZINE, seen earlier this
session). Fixed both angles regardless: added a 25MB streaming response-size cap as
defense-in-depth against a genuinely huge page, and broadened the exception handling to
catch `MemoryError` explicitly so it fails just that one page instead of crashing the
entire crawl. Verified with 7 unit-style checks (mocked responses) covering both cap paths,
the MemoryError catch, and confirming every pre-existing rejection path is unaffected.
Logged in `ISSUES_LOG.md`.

## 19. Why GRID-INDIA's daily PSP report was never downloaded (crawler doesn't work well against JS-heavy sites)

Investigated `grid-india.in/en/reports/daily-psp-report`, a page the user reported never
gets downloaded and takes a long time to load. Fetched it directly and found a pure
client-side React SPA shell with zero content in its raw HTML — all real content, including
the actual PDF/XLS report links, only exists after JS runs and calls a separate backend API
(`webapi.grid-india.in`). Initial diagnosis (tier-1 "succeeds" emptily, never escalates)
turned out to be incomplete once checked against real crawl history: tier 1 actually fails
outright for this whole site (same `CERTIFICATE_VERIFY_FAILED` SSL error as AERC), so the
site already correctly runs on tier 2 for everything. The real reasons the page was missed:
(1) it's simply never linked from the crawler's reachable pages — the homepage defaults to
Hindi and only exposes `/hi/` report links, and even that Hindi variant wasn't reached
within one 317-page run; (2) even when visited, the page's multi-step API-call loading
pattern needs more time than the default adaptive JS-wait allows before giving up. Fixed
all three for GRID-INDIA specifically (explicit seeds for both locale variants,
`use_fixed_js_wait: true`, `domain_blind_documents: true`) and verified end-to-end against
the live page: 8 of 8 document links now get recorded, versus 0 before.

Went through the user's own reference document describing how another crawler separates
"is this URL in scope to crawl?" from "is this a document I should download regardless of
its domain?" — confirmed this codebase already implements exactly that principle, just as
an opt-in per-site flag rather than the reference's recommended default. The user didn't
want future per-site flags to be something they have to remember to add reactively (the
whole problem with GRID-INDIA was that it was only caught because it got rechecked), so two
automatic, zero-config safety nets were built instead: (1) a new per-page check in the main
crawl loop that automatically re-verifies any zero-link tier-1 page via tier 2, without
touching the site's overall sticky tier decision (avoiding the false-escalation problem the
existing seed-probe-only version was designed around) — capped at tier 2 deliberately, not
tier 3, since that launches a visible browser; (2) flipped `domain_blind_documents`'s
default to `true` for newly-generated configs. Verified both thoroughly: 13 unit/integration
checks (mocked, no real network) covering the happy path, the false-positive-avoidance
case, no-regression on normal pages, and the tier-2 cap — plus confirmed which of the ~37
existing site configs would actually be affected by the default change (8 older configs
predating the flag, disclosed rather than silently changed). All logged in detail in
`ISSUES_LOG.md`.

## 20. GRID-INDIA's downloads mostly failed too (2470 of 2473) — same SSL class as AERC, fixed automatically this time

After the discovery fixes worked, almost every download failed with AERC's exact
`CERTIFICATE_VERIFY_FAILED` error, now on `webcdn.grid-india.in`. The user explicitly ruled
out another manual per-site config flag for this — wanted it handled automatically for any
future site, since there's no way to predict which one will hit it next. Built a config-free
fix: `download_pending_documents()` now builds a second, relaxed fallback session once per
run, and `download_one()` only tries it after the normal session fails with an `SSLError` —
zero cost for sites with no SSL problems. Caught and fixed a real bug along the way: the
first attempt tried `session.verify = False` after building a custom SSL adapter, which
doesn't work (Python's `ssl` module requires `check_hostname` disabled before `verify_mode`,
and a custom context bypasses the session-level `verify` flag entirely) — fixed by building
both settings directly inside the adapter's own context, gated so the existing NRLDC-only
usage (verification still on, just legacy renegotiation allowed) is unaffected. Verified with
9 unit/regression checks plus a real download of a previously-failing 5.18 MB PDF.

## 21. Added "First Run At" to the master summary, verified zero data loss on all 42 rows

Also corrected the user on stale info from earlier in this session — the master summary
wasn't at 16 rows anymore, it had grown to 42 real sites' worth of data. Dropped the
previously-discussed change-log database table idea entirely (not needed). Added a
`get_first_run()` function and a new "First Run At" column, positioned before the existing
"Last Run At". Given explicit concern about data loss, took real precautions rather than just
asserting safety: made a file backup and a full JSON snapshot of all 42 rows before touching
anything, confirmed the existing column-migration logic (built for a past real bug) preserves
every value by name, then after refreshing all 42 sites so the new column wouldn't sit blank
for ones not naturally re-run soon, diffed the full before/after state — zero mismatches on
any pre-existing column, all 42 sites intact, all 42 now correctly populated.

## 22. Recheck request surfaced 20 sites missing from the master summary entirely — not from this session's mistake

Asked to recheck whether the master summary was current. It was (zero crawls had run since
Sep 9), but comparing the database against the Excel turned up 20 sites with real, sometimes
large amounts of history (CERC alone: 21,393 documents) missing from the sheet entirely. The
user initially attributed this to the earlier smoke-test mistake in this session — corrected
that: every one of the 20 sites' latest run predates this whole conversation (Aug 27-29),
matching the pre-existing "only 16 of 37 sites" gap flagged at the very start of this session,
not the UERC-only smoke-test incident (which was already fixed and verified at the time).
Confirmed none of the 20 sites' underlying data was lost, then inserted all 20 as new rows at
the top of the sheet per the user's explicit request (a one-off manual recovery, not a change
to `update_master_summary()`'s normal append-at-bottom behavior), verified with a full
before/after diff against a fresh backup — all 42 existing rows untouched and in original
position, 20 new rows correctly added on top.

## 23. Audited every site with a near-0% download success rate; found and fixed TNERC's different problem

Asked whether the SSL fix had actually been found/fixed everywhere, with a list for
re-running. Found 6 sites with under 20% download success: AERC, CSERC, DERC, MSERC,
GRID-INDIA all share the exact SSL cert error already fixed automatically last session — just
need rerunning. TNERC was genuinely different (1088 uniform "Read timed out" failures, zero
SSL) and not yet covered. Also checked whether the SSL fix had produced any real results yet
— it hadn't, since zero crawler runs of any kind had happened since it was implemented on
Sep 10; the only evidence was the earlier isolated single-file test. Fixed TNERC's timeout
problem with the same automatic, config-free pattern as the SSL fallback: retry once with a
90s timeout on the same session after the normal 30s times out. Verified with 5 unit checks,
confirmed zero regression to the SSL fallback, and a real download of one of TNERC's actual
timing-out URLs (230KB PDF) now succeeds.
