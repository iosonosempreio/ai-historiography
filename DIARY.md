# Collection diary

A running log of what happened while collecting GitHub metadata with `github_repo_metadata.py`. The collection date used for all files is **2026-11-02** (UTC).

## Day 1 (2 to 3 November): the main run

- The script runs over the whole repository list in one batch: stars, commits, issues and pull requests for each repository, one repository after another.
- Most repositories finished in 10 to 40 minutes each.
- The two biggest took far longer. The second-to-last repository took **18 h 32 min** (61,633 commits, 38,505 issues, 109,256 PRs). Its PR download was throttled by GitHub's secondary rate limit, which kept the page size at 10 for hours.
- The last repository in the list (an even bigger one, about 125k commits, 63k issues and 127k PRs) had not started yet.

## Running the last repository in parallel

The goal was to start the last repository while the main run was still busy, without breaking the main run's logs or checkpoints. Findings from reading the script:

- `batch_run.log` is written to the **current working directory**, so running from another folder gives a separate log.
- `output/.state/batch.state.json` is **deleted when a run ends**, which could remove the main run's checkpoint. A second run should therefore use its own output folder.
- A new batch would be dated with today's date. To keep the files on the same collection date, the batch state file was copied into the second run's output folder.

Setup used:

1. A separate working folder next to the project (outside the repository, so git stays clean).
2. A `repos.txt` listing only the last repository.
3. A copy of `batch.state.json` inside that folder's own output directory.
4. Run with a **different GitHub token**, passed inline on the command line (`GITHUB_TOKEN=... python ... --repos repos.txt --out-dir ./out`) so it never goes in a shell profile or `.env`.

Caveat noted: the main run had already read the repository list, so it would eventually reach the last repository too. The plan was to move the finished files into the main output folder before that, or stop the main run at that point and run only the summary step (`--only summary`).

A second idea, moving the second-to-last repository's PR download to another token, was dropped. PR download follows one cursor, so two processes cannot split it. It only makes sense when the second token belongs to a different account, since rate limits are per account.

## Day 2 (3 November): failures on the huge repository

- The separate run got through commits and issues, then the PR download died after about 43k of 127k PRs. The error was **HTTP 502/504** from GitHub, six attempts in a row. The checkpoint was kept, so nothing was lost.
- Meanwhile the main run finished the second-to-last repository and started the last one from scratch, duplicating the work. It was stopped with Ctrl+C, and its leftover partial files were removed.

## Day 3 (4 November): diagnosing the timeouts

Retrying the same command kept failing, now with the page size already at its minimum of 10.

**Why:** 502/504 means GitHub's gateway gave up waiting for the query (about 10 seconds). The PR query asks for a lot per PR (labels, comment and review counts, the last commit's CI status, linked issues and cross-reference events). On a repository this large, a page of 10 PRs took 9 to 10 seconds, right at the limit.

**Was one PR to blame?** No. Running the script's query on the suspicious PRs one at a time took about 1 second each, even for the heaviest one (599 commits, 2,555 changed files, 733 cross-references). GitHub's status page showed no incident. That stretch of the repository's history is simply expensive to page through.

**Fix (committed):**

- Page sizes can now go down to 1 (100, 50, 25, 10, 5, 2, 1).
- Up to 10 retries, with waits capped at 120 seconds.
- The page size now **grows back** one step after 50 calm pages in a row. Without this, a single bad stretch left the rest of the download at page size 1, which was far too slow.
- README updated to match.

After restarting with the fix, the run passed the problem spot immediately, and the page size climbed from 1 to 2 within minutes. The fix worked as intended.

## Side note: the data keeps growing while we collect it

The biggest repository is so active that its totals moved during the download itself. That is why the progress line's total (`.../127,053 PRs`) kept creeping up between restarts.

| What | Value at the start of its download (3 Nov) | During or after (3 to 4 Nov) | Live check (4 Nov, 11:22 UTC) |
|---|---|---|---|
| Issues | 62,641 | 62,677 when the issue download finished a few hours later | 62,803 |
| Pull requests | 126,861 | 127,053 (3 Nov, evening), then 127,322 (4 Nov, morning) | 127,370 |
| Commits (default branch) | n/a | 124,658 when the commit download finished (3 Nov, afternoon) | 125,215 |
| Open issues | n/a | 4,870 (our summary), 4,875 (the colleague's summary, a day later) | 4,881 |
| Open PRs | n/a | 2,864 (our summary), 2,895 (the colleague's summary, a day later) | 2,910 |

Rough growth rates:

- **Pull requests:** about +509 in roughly 18 hours since the PR download started, so **about 27 new PRs per hour, around 650 per day**.
- **Issues:** about +162 in roughly 22 hours, so around 7 per hour. Issues and PRs share one numbering sequence on GitHub, so the two numbers are not independent.
- **Commits:** about +557 in a day.

What this means for the data:

- Downloads go from the oldest item to the newest, so new items land at the end. A long download therefore includes items created after the nominal collection date. In files labelled `2026-11-02`, the newest item is already dated **2026-11-03** for the second-to-last repository (PRs, issues and commits) and for the last one (issues, commits). Repositories that finished early on 3 Nov, such as the one that took 17 minutes, end on 2026-11-02.
- The last repository's PR file will go on growing until its download ends, so it will reach 4 Nov or later.
- Every file keeps the label `2026-11-02`, but the real end of each dataset differs by repository, from 2 Nov to 4 Nov.
- **Open decision:** for analysis, cut every dataset at the same date (for example keep only items dated up to and including 2026-11-02), so all repositories cover the same window. The extra rows are not wrong, they are just from a later moment.

## Day 3, later (4 November): finishing the collection

**The colleague's download was not complete either.** A colleague had also downloaded the last repository overnight, and the first plan was to merge their files. Looking inside showed that their data was dated 2026-11-03 and that their PR file was still an unfinished `.partial` file, stopped at about 42.8k PRs. It had stalled at the same spot as ours. Our own unfinished file was further along (45,678 PRs) and both start from the oldest PR, so there was nothing to merge. Their files were not used.

**Resuming our own download with the fixed script.**

- The run continued from the saved checkpoint at 45,678 PRs. In the heavy stretch the log showed the page size dropping on every 502 and growing back afterwards (it went down to 5 and up to 50), and the download still moved forward steadily.
- A later restart continued from 110,293 PRs and finished in 1 h 35 min with no failures: **127,520 PRs**, all with unique IDs, from 2025-11-26 to 2026-11-04.
- A small change was made along the way: the progress line now shows the current page size (for example `pull requests: 60,743/127,367 PRs (47%) - page size 10`), except for daily stars, which use a fixed page.

**Merging into the main collection.**

1. The five files of the last repository (stars, commits, complete commit history, issues, PRs) were moved into the main output folder with `mv -n`, which keeps the names and never overwrites an existing file.
2. The summary step (`--only summary`) was run on the whole list. It filled in the missing rows in under a minute and removed the batch checkpoint itself, so the run closed cleanly.

**Final check.** Of the 60 repositories in the list, **58 are complete**: all five files exist, there are no leftover `.partial` files, and each has a row in `repos_summary_2026-11-02.csv`.

**Two repositories could not be collected:** `asafelobotomy/ClosedClaw` and `mcotdev/femtoclaw`. GitHub returns a 404 for both (deleted, private or renamed, with no redirect). They failed the same way in the very first batch run on 2 Nov, so there never was data for them. They stay in the list, as part of the record of what was sampled.

**Totals added to the summary.** The summary only had the number of *open* issues and PRs, so two columns were added: `issues_amount` (all issues, open and closed, pull requests not included) and `prs_amount` (all pull requests, whether open, closed or merged). They cost nothing extra, since they ride along in the same single request per repository. The README documents them.

- A safety check was added to the script: if an existing summary file has a different set of columns, it is rebuilt instead of being extended. Appending new rows to the old layout would have produced a broken CSV.
- The summary was regenerated on 4 Nov (about a minute, 61 GraphQL points) and kept the `2026-11-02` label. Again 58 rows, with the same two repositories reported as not found.
- For the biggest repository it now shows **62,844 issues** and **127,531 PRs** in total, with 4,861 issues and 2,881 PRs still open. These are live numbers from 4 Nov, so they are slightly higher than the row counts in the downloaded files (see the side note on growth). The open counts can also go down, as items get closed.

### Still to do (done by hand)

1. Delete the separate working folder and the colleague's copy of the data. Both are now redundant.
2. Pick the cutoff date for analysis (see the side note above: items up to and including 2026-11-02).
3. Decide whether the two missing repositories stay in `repos.txt`. If they stay, a future full run will report them as `FAILED` again; that is expected.

## Lessons learned

- Run a second process in a **separate folder and output directory**. Logs and batch state are shared otherwise.
- Timeouts on very large repositories are normal. A resumable checkpoint, smaller pages and patient retries are what make the download finish.
- A fetch that only shrinks its page size gets slower forever. It needs to grow back.
- Check GitHub's status page and test single items before suspecting a specific record.
- Before relying on someone else's download, open the folder: a `.partial` file or a different date in the name means it is unfinished or from another collection.
- Check the whole collection at the end (every repository has every file and a summary row) instead of trusting that no error appeared.
- Very large repositories keep growing during a long download, so choose one cutoff date for the analysis.
