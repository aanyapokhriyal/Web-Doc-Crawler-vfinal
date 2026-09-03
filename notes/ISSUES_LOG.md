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
