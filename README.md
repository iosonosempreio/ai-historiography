# ai-historiography

`github_repo_metadata.py` downloads the history of a list of GitHub repositories (stars, commits, issues and pull requests) and saves it as CSV files that you can open in Excel, LibreOffice, pandas, R or any other tool.

## Contents

- [a. What it does](#a-what-it-does)
- [b. Requirements](#b-requirements)
- [c. Setup in a virtual environment](#c-setup-in-a-virtual-environment)
- [d. Token](#d-token)
- [e. Usage](#e-usage)
- [f. Output reference](#f-output-reference)
- [g. Glossary of categorical values](#g-glossary-of-categorical-values)
- [h. How it works](#h-how-it-works)
- [i. Known limitations](#i-known-limitations)
- [j. Troubleshooting](#j-troubleshooting)

## a. What it does

You give the script a text file listing repositories (one `owner/repo` per line). For each repository, in this order, it downloads the star history, the commit history of the default branch, every issue and every pull request since the repository was created, and saves each of them as a CSV file. After the last repository it writes one summary file with a row per repository. Everything is checkpointed: if the run is interrupted (Ctrl+C, a closed laptop, a lost connection), running the same command again continues where it stopped.

Output files, with example names for `huggingface/transformers` collected on 2 November 2026 (`[user]` is the repository owner, the date is the day of collection):

| File | One row per | Example file name |
|---|---|---|
| Star history | calendar day | `huggingface-transformers_stars_history_2026-11-02.csv` |
| Star history, star by star (only when GitHub allows it, see [Known limitations](#i-known-limitations)) | star | `huggingface-transformers_stars_history_2026-11-02_complete_history.csv` |
| Commit history | calendar day | `huggingface-transformers_commits_history_2026-11-02.csv` |
| Commit history, commit by commit | commit | `huggingface-transformers_commits_history_2026-11-02_complete_history.csv` |
| Issues | issue | `huggingface-transformers_issues_history_2026-11-02.csv` |
| Pull requests | pull request | `huggingface-transformers_pull_requests_history_2026-11-02.csv` |
| Summary of the whole batch | repository | `repos_summary_2026-11-02.csv` |

The script also appends one line per repository to `batch_run.log` (in the folder you run it from), so you can see afterwards what was downloaded and what failed.

> **Important: GitHub changed how star data can be accessed in 2026.** Since July 2026 GitHub shows *who* starred a repository only to that repository's admins and collaborators. The daily star history file is still produced for every public repository, because it comes from the privacy-safe star-history service GitHub added in September 2026, and it is exact. The star-by-star file (stargazer names and times) is produced only for repositories your account administers or collaborates on. For all others it is skipped, and the log says so.

## b. Requirements

- **Python 3.9 or newer.** Check with `python3 --version` (macOS / Linux) or `py --version` (Windows). Download it from [python.org](https://www.python.org/downloads/) if needed.
- **A GitHub personal access token.** It is free and takes two minutes to create (see [Token](#d-token)).
- An internet connection. The two Python libraries the script needs, `requests` and `pandas`, are installed in the next step.

## c. Setup in a virtual environment

A *virtual environment* (venv) is a private folder of Python libraries for this project, so nothing is installed system-wide. Open a terminal in the project folder (the folder that contains `github_repo_metadata.py`) and run:

**macOS / Linux**

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**Windows (PowerShell)**

```powershell
py -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

- When the environment is active, `(venv)` appears at the start of the terminal prompt. All the commands below assume it is active.
- To leave the environment, type `deactivate`.
- The first two lines are needed only once. In a **new terminal**, repeat only the activate line (`source venv/bin/activate` or `venv\Scripts\Activate.ps1`) before running the script.
- If `pip` answers **"externally-managed-environment"**, or Python says **"No module named requests"** (or `pandas`), the virtual environment is not active: run the activate line again.
- On Windows, if PowerShell refuses to run `Activate.ps1` ("running scripts is disabled on this system"), run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once and try again.

## d. Token

GitHub requires a token for its API. A token with no permissions is enough, because the script only reads public data.

1. Go to [github.com/settings/tokens](https://github.com/settings/tokens) and choose **Generate new token → Generate new token (classic)**.
2. Give it a name (e.g. `repo-metadata`), choose an expiration date, and **do not tick any scope**: no scopes is enough for public repositories.
3. Click **Generate token** and copy it (it starts with `ghp_`). GitHub shows it only once.

Set the token for the current terminal session only. It is forgotten when you close the terminal, so repeat this in each new terminal:

```bash
# macOS / Linux
export GITHUB_TOKEN=ghp_xxx
```

```powershell
# Windows PowerShell
$env:GITHUB_TOKEN="ghp_xxx"
```

(replace `ghp_xxx` with your token).

> **Keep the token secret.** Never paste it into code, chats, issues, e-mails or commits. If it is ever exposed, revoke it immediately at [github.com/settings/tokens](https://github.com/settings/tokens) and create a new one. The script reads the token only from `GITHUB_TOKEN` and never prints or saves it.

## e. Usage

First create the list of repositories, a plain text file (by default `repos.txt`, next to the script) with one `owner/repo` per line. Blank lines and lines starting with `#` are ignored, duplicates are removed, and pasted links such as `https://github.com/pytorch/pytorch` are accepted:

```text
# Deep learning frameworks
pytorch/pytorch
tensorflow/tensorflow
huggingface/transformers
```

Then, with the virtual environment active and `GITHUB_TOKEN` set:

| What you want | Command |
|---|---|
| Full batch: every repository in `repos.txt`, all kinds of data, then the summary | `python github_repo_metadata.py` |
| A single repository: put just that line in a file of its own | `python github_repo_metadata.py --repos one_repo.txt` |
| Only the issues of every repository in the list | `python github_repo_metadata.py --only issues` |
| Start over, ignoring files already downloaded today and any saved progress | `python github_repo_metadata.py --force` |
| Save the files in another folder (default `./output`) | `python github_repo_metadata.py --out-dir ./data/2026-11` |
| Different list and folder together | `python github_repo_metadata.py --repos ai_repos.txt --out-dir ./ai_output` |

`--only` accepts `stars`, `commits`, `issues`, `prs` or `summary`. Options can be combined, e.g. `python github_repo_metadata.py --only prs --force`.

To create a one-line list file: `echo "huggingface/transformers" > one_repo.txt` (macOS / Linux) or `Set-Content one_repo.txt "huggingface/transformers"` (PowerShell).

**Long runs.** A batch of large repositories can take several hours (see [How it works](#h-how-it-works)).

- On macOS, prefix the command with `caffeinate -i` so the computer does not go to sleep while the script runs:

  ```bash
  caffeinate -i python github_repo_metadata.py
  ```

- Use a terminal window you will not close until the run ends. If it does get closed, or the computer sleeps, nothing is lost: run the same command again to continue.

While it runs, the script shows one progress line per repository and kind of data, for example `issues: 4,200/12,811 issues (32%)`.

## f. Output reference

General conventions for all files:

- UTF-8, comma-separated, with a header row. Text containing commas, quotes or line breaks is quoted, so every file opens correctly in pandas, R or a spreadsheet.
- **Dates** of opening or creation are `YYYY-MM-DD`. All other timestamps are full ISO 8601 in UTC, e.g. `2024-05-01T13:45:12Z`, and are empty when they do not apply (for example `closed_at` of an open issue). All days are UTC days, and the date in each file name is the UTC date on which the batch started.
- **Lists** in one cell (labels, assignees, links, languages, topics) are separated by semicolons, e.g. `bug;help wanted`.
- **Booleans** are `true` or `false`.
- **Categorical values** (`state`, `user_role`, ...) are GitHub's own values, exactly as returned by the API (see the [glossary](#g-glossary-of-categorical-values)).
- **Deleted accounts** appear as `ghost`, as on github.com.
- **Links between issues and pull requests** are written as the bare number when they point to the same repository (`1234`) and as `owner/repo#number` when they point to another one (`huggingface/datasets#5678`).

### `[user]-[repo]_stars_history_[date].csv`

One row per calendar day, from the day the repository was created (or its first star, if earlier) to the collection date. Days without new stars repeat the previous total.

| Column | Type | Description | Example |
|---|---|---|---|
| `date` | date | Calendar day (UTC) | `2021-03-14` |
| `stars_amount` | integer | Total stars at the end of that day. The last row equals the repository's star count at collection time | `98213` |

### `[user]-[repo]_stars_history_[date]_complete_history.csv`

One row per star, oldest first. **Only produced for repositories whose stargazer list GitHub shows to your account** (repositories you administer or collaborate on, see [Known limitations](#i-known-limitations)).

| Column | Type | Description | Example |
|---|---|---|---|
| `user` | text | Login of the account that starred the repository | `octocat` |
| `timestamp` | timestamp | When the star was given (UTC) | `2021-03-14T09:26:53Z` |
| `cumulative_count` | integer | Running number of stars, 1 for the first star | `1542` |

### `[user]-[repo]_commits_history_[date].csv`

One row per calendar day from the first commit to the collection date, including days with zero commits. Counts use the *committed* date (UTC) and cover the default branch only.

| Column | Type | Description | Example |
|---|---|---|---|
| `date` | date | Calendar day (UTC) | `2019-07-02` |
| `commits_amount` | integer | Commits committed that day | `14` |
| `cumulative_commits` | integer | Commits up to and including that day. The last row equals the number of commits on the default branch | `3820` |

### `[user]-[repo]_commits_history_[date]_complete_history.csv`

One row per commit of the default branch, newest first (Git history order). No e-mail addresses are collected.

| Column | Type | Description | Example |
|---|---|---|---|
| `sha` | text | Commit identifier | `9fceb02d0ae598e95dc970b74767f19372d61af8` |
| `url` | text | Link to the commit on github.com | `https://github.com/owner/repo/commit/9fceb02...` |
| `authored_date` | timestamp | When the change was written (UTC) | `2019-07-02T10:14:00Z` |
| `committed_date` | timestamp | When the commit was applied to the history (UTC). It differs from `authored_date` after a rebase, cherry-pick or squash | `2019-07-02T11:20:41Z` |
| `author_login` | text | GitHub login of the author; empty if the commit's author e-mail is not linked to a GitHub account | `octocat` |
| `author_name` | text | Author name recorded in the commit | `Mona Lisa` |
| `author_type` | category | GitHub account type of the author (see [glossary](#author_type)); empty if not linked | `User` |
| `committer_login` | text | GitHub login of the committer; empty if not linked, which is the case for commits made or merged through the github.com web interface (their committer is "GitHub") | `octocat` |
| `message_headline` | text | First line of the commit message, as GitHub shows it (GitHub shortens very long first lines and ends them with `…`) | `Fix typo in README` |
| `additions` | integer | Lines added | `12` |
| `deletions` | integer | Lines removed | `3` |
| `changed_files` | integer | Files changed; empty when GitHub cannot compute it (very large commits) | `2` |
| `parents_count` | integer | Number of parent commits. Greater than 1 means a merge commit | `1` |

### `[user]-[repo]_issues_history_[date].csv`

One row per issue since the repository was created, open and closed, oldest first. Pull requests are not included (they have their own file).

| Column | Type | Description | Example |
|---|---|---|---|
| `opened_date` | date | Day the issue was opened (UTC) | `2022-01-17` |
| `issue_id` | integer | Issue number, as in `github.com/owner/repo/issues/157676` | `157676` |
| `url` | text | Link to the issue | `https://github.com/owner/repo/issues/157676` |
| `state` | category | `OPEN` or `CLOSED` ([glossary](#state)) | `CLOSED` |
| `state_reason` | category | Why the issue is in its state ([glossary](#state_reason)); empty for issues that were never closed | `COMPLETED` |
| `title` | text | Issue title | `Crash when loading model, "out of memory"` |
| `opened_by` | text | Login of the author (`ghost` for deleted accounts) | `octocat` |
| `author_type` | category | Account type of the author ([glossary](#author_type)); empty for `ghost` | `User` |
| `labels` | list | Labels, semicolon-separated | `bug;good first issue` |
| `comments_amount` | integer | Number of comments | `7` |
| `reactions_amount` | integer | Total reactions on the issue itself (all emoji together) | `23` |
| `assignees` | list | Logins of the assignees | `alice;bob` |
| `milestone` | text | Milestone title, empty if none | `v2.0` |
| `is_locked` | boolean | Whether the conversation is locked | `false` |
| `closed_at` | timestamp | When it was last closed (UTC); empty if open | `2022-02-01T08:00:12Z` |
| `updated_at` | timestamp | Last update of any kind (UTC) | `2023-06-30T19:12:45Z` |
| `last_comment_date` | timestamp | Date of the most recent comment (UTC); empty if no comments | `2022-01-31T22:05:09Z` |
| `closing_links` | list | Pull requests linked to this issue as closing it, through closing keywords such as "Fixes #12" or the "Development" sidebar. Includes PRs that were closed without merging; check their `state` in the pull-request file | `1290;1305` |
| `mentioned_links` | list | Pull requests that mention this issue ("… mentioned this issue" in its timeline), excluding those already in `closing_links` | `1288;huggingface/datasets#5678` |

### `[user]-[repo]_pull_requests_history_[date].csv`

One row per pull request since the repository was created, in every state, oldest first.

| Column | Type | Description | Example |
|---|---|---|---|
| `opened_date` | date | Day the PR was opened (UTC) | `2022-01-18` |
| `pr_id` | integer | PR number | `1290` |
| `url` | text | Link to the PR | `https://github.com/owner/repo/pull/1290` |
| `state` | category | `OPEN`, `CLOSED` or `MERGED` ([glossary](#state)) | `MERGED` |
| `is_draft` | boolean | Whether the PR is a draft *now* (at collection time) | `false` |
| `title` | text | PR title | `Fix memory leak in loader` |
| `opened_by` | text | Login of the author (`ghost` for deleted accounts) | `octocat` |
| `author_type` | category | Account type of the author ([glossary](#author_type)); empty for `ghost` | `Bot` |
| `user_role` | category | The author's relationship with the repository *at collection time* ([glossary](#user_role)) | `CONTRIBUTOR` |
| `labels` | list | Labels, semicolon-separated | `bug;needs review` |
| `comments_amount` | integer | Conversation comments. Review comments written on lines of code are not counted | `4` |
| `reviews_count` | integer | Reviews submitted (approvals, change requests and review comments) | `2` |
| `review_decision` | category | Review status required by the repository's rules ([glossary](#review_decision)); empty when none applies | `APPROVED` |
| `base_branch` | text | Branch the PR was opened against | `main` |
| `is_cross_repository` | boolean | `true` if the PR comes from a fork | `true` |
| `additions` | integer | Lines added | `120` |
| `deletions` | integer | Lines removed | `48` |
| `changed_files` | integer | Files changed | `5` |
| `commits_count` | integer | Commits in the PR | `3` |
| `merged_by` | text | Login of who merged it; empty if not merged (`ghost` if that account was deleted) | `maintainer1` |
| `ci_status` | category | Combined status of the checks on the PR's last commit ([glossary](#ci_status)); empty if no checks ran | `SUCCESS` |
| `closed_at` | timestamp | When it was closed or merged (UTC); empty if open | `2022-01-20T10:00:00Z` |
| `merged_at` | timestamp | When it was merged (UTC); empty if not merged | `2022-01-20T10:00:00Z` |
| `updated_at` | timestamp | Last update (UTC) | `2022-01-20T10:05:31Z` |
| `closing_links` | list | Issues this PR is linked to as closing them (closing keywords or the "Development" sidebar) | `1288` |
| `mentioned_links` | list | Issues that mention this PR ("… mentioned this pull request" in its timeline), excluding those in `closing_links` | `1201;huggingface/datasets#77` |

There is no separate "merged or not" column: `state` already says `MERGED`, and equivalently a merged PR has a `merged_at` value (see the [glossary](#state)).

### `repos_summary_[date].csv`

One row per repository of the batch: a snapshot at collection time.

| Column | Type | Description | Example |
|---|---|---|---|
| `repo` | text | `owner/repo` as written in your list | `huggingface/transformers` |
| `owner_type` | category | `User` or `Organization` | `Organization` |
| `created_at` | date | Day the repository was created (UTC) | `2018-10-29` |
| `pushed_at` | timestamp | Last push to any branch (UTC) | `2026-11-02T08:12:40Z` |
| `description` | text | Repository description | `Transformers: the model-definition framework…` |
| `primary_language` | text | Main language detected by GitHub | `Python` |
| `languages` | list | Every detected language with its size in bytes, largest first | `Python:61234567;Cuda:120034` |
| `license` | text | SPDX license identifier; empty if none, `NOASSERTION` if GitHub cannot identify it | `Apache-2.0` |
| `topics` | list | Repository topics | `nlp;pytorch;deep-learning` |
| `stars_amount` | integer | Stars | `155000` |
| `forks_amount` | integer | Forks | `31000` |
| `watchers_amount` | integer | Accounts watching the repository (notifications), not stars | `1100` |
| `open_issues_amount` | integer | Open issues (pull requests not included) | `1900` |
| `issues_amount` | integer | All issues, open and closed (pull requests not included) | `9800` |
| `open_prs_amount` | integer | Open pull requests | `950` |
| `prs_amount` | integer | All pull requests, whether open, closed or merged | `14500` |
| `disk_usage_kb` | integer | Repository size on GitHub in kilobytes | `512340` |
| `is_fork` | boolean | `true` if the repository is a fork | `false` |
| `parent_repo` | text | The repository it was forked from; empty if not a fork | `` |
| `is_archived` | boolean | `true` if archived (read-only) | `false` |
| `default_branch` | text | Name of the default branch | `main` |

## g. Glossary of categorical values

All categorical values are **GitHub's native values, exactly as returned by its API**: the script never translates, lowercases or relabels them. Each table lists every value GitHub's GraphQL schema defines for the column (checked on 2 November 2026). The last column marks the values that actually occurred in the data collected on 2 November 2026 (about 50 repositories: 9,500 issues, 12,900 pull requests and 49,000 commits). The other values are documented by GitHub but did not occur there.

### `state`

Issues (`IssueState`):

| Value | Meaning | Seen in collected data |
|---|---|---|
| `OPEN` | The issue is open | ✓ |
| `CLOSED` | The issue is closed (see `state_reason` for why) | ✓ |

Pull requests (`PullRequestState`):

| Value | Meaning | Seen in collected data |
|---|---|---|
| `OPEN` | Still open: not merged, not closed | ✓ |
| `CLOSED` | Closed **without** being merged | ✓ |
| `MERGED` | Closed by being merged. Merged PRs always have `merged_at`; a `CLOSED` PR never does | ✓ |

### `state_reason`

Issues only (`IssueStateReason`). The script asks GitHub for the detailed variant, so duplicates appear as `DUPLICATE` instead of being folded into `NOT_PLANNED`.

| Value | Meaning | Seen in collected data |
|---|---|---|
| *(empty)* | The issue has never been closed (or GitHub recorded no reason) | ✓ |
| `COMPLETED` | Closed as done or fixed | ✓ |
| `NOT_PLANNED` | Closed as "won't fix", stale, or not going to be done | ✓ |
| `DUPLICATE` | Closed as a duplicate of another issue | ✓ |
| `REOPENED` | Closed at some point and then reopened: the issue is open again (in the collected data every `REOPENED` issue was `OPEN`). This is why `state_reason` is not always empty for open issues | ✓ |

### `author_type`

The GitHub account type (`Actor` types) of an issue or PR author or a commit author.

| Value | Meaning | Seen in collected data |
|---|---|---|
| `User` | A personal GitHub account | ✓ |
| `Bot` | An app/bot account, e.g. `dependabot`, `github-actions`, `pre-commit-ci` (logins usually end in `[bot]`) | ✓ |
| `Organization` | An organization account | |
| `Mannequin` | A placeholder for a user whose content was imported (e.g. migrated from another platform) and who has not claimed it | |
| `EnterpriseUserAccount` | An account managed by a GitHub Enterprise | |
| *(empty)* | Deleted account (`opened_by` is `ghost`), or a commit author not linked to any GitHub account | ✓ |

In the commit files GitHub can only link authors to personal accounts, so `author_type` there is always `User` or empty, even for bot commits (whose login still ends in `[bot]`).

### `user_role`

Pull requests only: GitHub's `authorAssociation`, the author's relationship with the repository **at collection time** (see [Known limitations](#i-known-limitations)).

| Value | Meaning | Seen in collected data |
|---|---|---|
| `OWNER` | Owns the repository | ✓ |
| `MEMBER` | Member of the organization that owns the repository (only if the membership is public, or visible to your token) | ✓ |
| `COLLABORATOR` | Invited to collaborate on the repository | ✓ |
| `CONTRIBUTOR` | Has previously committed to the repository | ✓ |
| `FIRST_TIME_CONTRIBUTOR` | Has not previously committed to this repository | |
| `FIRST_TIMER` | Has not previously committed anywhere on GitHub | |
| `MANNEQUIN` | Placeholder for an unclaimed imported user | |
| `NONE` | No association with the repository | ✓ |

Because the role is computed at collection time, authors whose pull request was later merged count as `CONTRIBUTOR`. The two "first time" values only describe people who still had no merged commit on that day.

### `review_decision`

Pull requests only (`PullRequestReviewDecision`). It is filled in only when the repository's rules require reviews before merging.

| Value | Meaning | Seen in collected data |
|---|---|---|
| `APPROVED` | Received the required approving review(s) | ✓ |
| `CHANGES_REQUESTED` | A reviewer asked for changes | ✓ |
| `REVIEW_REQUIRED` | A review is required before it can be merged and has not been given | ✓ |
| *(empty)* | The repository does not require reviews, so GitHub reports no decision | ✓ |

### `ci_status`

Pull requests only: the combined state (`StatusState`) of all checks and statuses on the PR's last commit.

| Value | Meaning | Seen in collected data |
|---|---|---|
| `SUCCESS` | All checks passed | ✓ |
| `FAILURE` | At least one check failed | ✓ |
| `PENDING` | Checks are still running or queued | ✓ |
| `ERROR` | A check could not run (errored) | |
| `EXPECTED` | A required check has not reported yet | |
| *(empty)* | No checks or statuses ran on the last commit (common for old PRs, before CI existed) | ✓ |

## h. How it works

**GraphQL, one request at a time.** The script uses GitHub's GraphQL API for everything except the daily star history (see below), and sends strictly one request at a time with a short pause (0.1–0.2 s) between requests. Lists are fetched in pages of 100 items using GitHub's *cursors*: a bookmark meaning "continue after this item". Issues and pull requests are requested oldest first (by creation date) and the star list oldest first (by star date), so a saved cursor stays valid and new items can only appear after it. Commits come in Git history order (newest first) from the commit that was the head of the default branch when the fetch started, so new pushes cannot shift a saved cursor. Every request also asks GitHub how much of the hourly quota is left.

**Daily star history.** Since GitHub restricted stargazer lists (July 2026), the only source of star history for repositories you do not administer is GitHub's star-history REST endpoint (`/repos/{owner}/{repo}/stargazers/history`). It returns exact star counts per day, grouped by week. This is the one place where the script uses REST instead of GraphQL, because GitHub offers no GraphQL equivalent. It counts against the separate REST quota (5,000 requests/hour) and needs about one request per 30 weeks of repository age.

**Rate limits.**

- *Primary limit:* 5,000 GraphQL points per hour, shared by the whole batch. Most pages cost 1 to 5 points. When fewer than 50 points are left, the script prints the time it will wake up, sleeps until the quota resets (plus 5 seconds), then continues by itself.
- *Secondary limits:* GitHub also limits bursts (roughly 2,000 points per minute), concurrent requests (hence one request at a time), and the work a single query may cause (at most 500,000 nodes per query and about 10 seconds of execution). If GitHub signals a secondary limit, the script waits as long as GitHub asks (the `Retry-After` header), or otherwise 60 s, then 120 s, 240 s… up to 15 minutes between attempts.
- *Heavy queries:* if a page is too heavy (a timeout, "502/504", or a node or resource limit), the script automatically asks for smaller pages: 100, then 50, 25 and 10 items. Pull requests are the heaviest and often run at 50 per page.
- *Connection problems* (timeouts, dropped connections, 502/503/504 errors): each request is retried up to 10 times, waiting 2, 4, 8… up to 120 seconds in between, and the page size is lowered step by step down to a single item. If it still fails, the script logs `FAILED: owner/repo kind - reason`, keeps the progress saved for that repository and kind, and moves on to the next one.
- *Errors that cannot be fixed by retrying* (bad token, repository not found, private or deleted): the script logs `FAILED: owner/repo - reason` and moves on to the next repository. A single problem never stops the whole batch.

**Checkpointing.** A final CSV file is never written directly:

- While a fetch is running, rows are appended page by page to `<final name>.csv.partial` in the output folder, so memory use stays low even for repositories with hundreds of thousands of items.
- After every page, a small state file `<final name>.state.json` in `output/.state/` records the cursor of the last saved page, the number of items fetched, the original collection date, and the time of the last update. It is written to a temporary file and then renamed, so it is never half-written.
- When a fetch is complete, the daily files (stars and commits per day) are computed from the per-item data, the `.partial` file is renamed to its final name, and the `.partial` and `.state.json` files are deleted. **A file without `.partial` is therefore always complete.**

**Resuming.** Just run the same command again. For each repository and kind, the script continues from the saved cursor (rows written after the last checkpoint are discarded and fetched again, so there are no duplicates). It keeps the original collection date in the file names, even if you resume on a later day, and says so in the log. It also skips files that are already complete. The summary file is kept and completed with any repositories of your list that it does not contain yet, for example after you add repositories to the list on the same day. If GitHub no longer accepts a saved cursor, that repository and kind restart from the beginning, and the log says so. `--force` ignores existing files and saved progress and starts over with today's date. If you interrupted a run that used `--force`, resume it **without** `--force` (the script prints the exact command when you press Ctrl+C).

**Rough runtime.** One request returns up to 100 commits, issues or pull requests (pull requests often 50), so time and quota grow with the size of each repository. Measured on a real batch of 48 repositories (44,500 commits, 6,500 issues and 12,600 pull requests took 81 minutes and 1,840 points):

- Commits: about 1 point and 5 seconds per 100 commits.
- Issues: about 8 points and 18 seconds per 100 issues (issues with many links need extra requests).
- Pull requests: about 3 points and 11 seconds per 100 PRs.
- Stars and summary: a few seconds and about 10 points per repository.

A small repository takes under a minute. The largest repository of that batch (12,000 commits, 1,400 issues, 1,300 PRs) took 16 minutes and 280 points. A very large one (100,000 commits, 30,000 issues, 20,000 PRs) would need about 4,000 points, nearly a full hour of quota, and three to four hours. A full batch over many large repositories can therefore take several hours or more, partly spent waiting for the hourly quota to reset. That is normal.

## i. Known limitations

- **Star-by-star history is restricted by GitHub.** Since July 2026 GitHub lists who starred a repository only to its admins and collaborators, so `..._stars_history_[date]_complete_history.csv` is produced only for those repositories (for your own repositories GitHub may require a token with the `public_repo` scope). The daily file `..._stars_history_[date].csv` is produced for every public repository. Its daily counts reflect the people who star the repository *now*, so stars that were later removed are not counted on the day they were given.
- `user_role` is GitHub's `authorAssociation` **at collection time**, not necessarily at the time the PR was opened. Someone's role over time cannot be recovered from GitHub.
- `MEMBER` can be hidden when someone's organization membership is private, so a core team member may appear as `CONTRIBUTOR` or `NONE`.
- `is_draft` is the current value: a PR that started as a draft and was later marked ready shows `false`.
- `closing_links` come from GitHub's closing references (closing keywords or the "Development" sidebar). `mentioned_links` come from GitHub's cross-references. Links written only in free text that GitHub did not register as references may be missed.
- Commits cover the default branch only: commits that exist only on other branches are not included. Commits whose author e-mail is not linked to a GitHub account have an empty `author_login`.
- Deleted accounts appear as `ghost`.
- The data is a snapshot taken on the collection date, not a live feed. Counts such as comments, reactions, labels and states are the values on that day.
- All days and dates are in UTC, so an event at 23:30 in New York is counted on the following UTC day.
- File names are built as `[user]-[repo]`, so `a/b-c` and `a-b/c` would produce the same names. In the unlikely case your list contains such a pair, run them separately with different `--out-dir` folders.

## j. Troubleshooting

| Problem | What to do |
|---|---|
| `Error: the GITHUB_TOKEN environment variable is not set` | Set the token in this terminal (see [Token](#d-token)). It must be set again in every new terminal. |
| `401 Bad credentials` | The token is wrong, expired or revoked. Copy it again, or create a new one. |
| `FAILED: owner/repo - repository not found` | Check the spelling in your list. The repository may be private, deleted or renamed: open `https://github.com/owner/repo` in a browser. Renamed repositories are usually followed automatically, and the log notes the new name. |
| `ModuleNotFoundError: No module named 'requests'` (or `pandas`) | The virtual environment is not active: run `source venv/bin/activate` (macOS / Linux) or `venv\Scripts\Activate.ps1` (Windows) and try again. |
| `Rate limit: ... Sleeping until 14:05:12 local time` | Normal: the hourly quota is used up. Wait (the script continues by itself), or press Ctrl+C and rerun the same command later. Nothing is lost. |
| `Secondary rate limit reached; waiting ...` | Normal: GitHub asked the script to slow down. It waits and continues. |
| `GitHub timed out (HTTP 502); retrying ...` or `Query too large for GitHub; retrying with page size 50` | Normal for large repositories: the script retries with smaller pages. |
| The run is very slow | Large repositories have tens of thousands of items, and the hourly quota limits the speed (see [How it works](#h-how-it-works)). Let it run. On macOS use `caffeinate -i`. |
| The run was interrupted (Ctrl+C, closed terminal, sleep, power cut) and there are `.partial` files | Run the same command again: it resumes from the last saved page. |
| `FAILED: owner/repo kind - ... gave up after 10 attempts` | A temporary network or GitHub problem. Rerun the same command later: it resumes that repository and kind. |
| You want to throw away saved progress (stale `.partial` / `.state.json` files) | Delete the `output/.state` folder and the `.partial` files, or run with `--force`. |
| `Resuming an unfinished batch started on ...` but you wanted fresh data | A previous batch did not finish, so its collection date is reused. Run with `--force` to start a new collection dated today. |
| `cannot write ...csv: is it open in another program?` (Windows) | Close the CSV file in Excel and rerun the command. |
