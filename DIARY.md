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

## End of the collection

A colleague managed to download the last repository's data overnight, so the separate run was stopped at about 43k of 127k PRs. Nothing is running now.

### Still to do

1. Copy the colleague's files into `output/`, keeping the script's naming pattern `<owner>-<repo>_<kind>_history_<date>.csv`. Use the `2026-11-02` date if possible, otherwise a later full run would treat them as missing and refetch.
2. Run `python github_repo_metadata.py --only summary` to add the last repository to the summary file.
3. Delete the separate working folder once the colleague's data is verified.

## Lessons learned

- Run a second process in a **separate folder and output directory**. Logs and batch state are shared otherwise.
- Timeouts on very large repositories are normal. A resumable checkpoint, smaller pages and patient retries are what make the download finish.
- A fetch that only shrinks its page size gets slower forever. It needs to grow back.
- Check GitHub's status page and test single items before suspecting a specific record.
