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
