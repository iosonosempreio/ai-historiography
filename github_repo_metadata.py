#!/usr/bin/env python3
"""
github_repo_metadata.py - download metadata for a list of GitHub repositories
and save it as CSV files.

    python github_repo_metadata.py [--repos repos.txt] [--out-dir ./output]
                                   [--only stars|commits|issues|prs|summary] [--force]

For every repository in the list the script saves, in this order, its star
history, the commit history of its default branch, its issues and its pull
requests. After the last repository it writes one summary file for the whole
batch. The GitHub token is read from the GITHUB_TOKEN environment variable.

Everything is fetched with GitHub's GraphQL API, one request at a time, with
one exception: since July 2026 GitHub shows the list of stargazers only to a
repository's admins and collaborators, and the replacement that works for
every public repository (daily star counts) exists only as a REST endpoint.

Every fetch is checkpointed: rows are appended page by page to
"<final name>.csv.partial" and the cursor of the last page is saved in
"<out-dir>/.state/<final name>.state.json". Running the same command again
resumes where the previous run stopped. See README.md for details.
"""
from __future__ import annotations

import argparse
import codecs
import csv
import datetime as dt
import json
import os
import random
import re
import shlex
import shutil
import signal
import sys
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple

import pandas as pd
import requests

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

GRAPHQL_URL = "https://api.github.com/graphql"
REST_URL = "https://api.github.com"
REST_API_VERSION = "2026-03-10"   # first REST API version documenting /stargazers/history
USER_AGENT = "github-repo-metadata-script"

PAGE_SIZES = (100, 50, 25, 10)    # items per GraphQL page, lowered step by step when GitHub struggles
STAR_HISTORY_PAGE_SIZE = 30       # weeks per page: the maximum the star history endpoint allows
STAR_HISTORY_MAX_PAGE = 100       # the endpoint serves at most 100 pages (about 57 years)

REQUEST_TIMEOUT_S = 60            # give up on a single request after this many seconds
MAX_ATTEMPTS = 6                  # transient errors: 6 attempts, waiting 2, 4, 8, 16, 32 s in between
SECONDARY_WAIT_S = 60             # secondary rate limit without Retry-After: wait 60, 120, 240 s ...
SECONDARY_WAIT_MAX_S = 15 * 60    # ... but never more than 15 minutes at a time
MAX_SECONDARY_WAITS = 8           # stop a repo/kind after this many secondary-limit waits in a row
RATE_LIMIT_FLOOR = 50             # below this many points left, sleep until the hourly reset
RESET_MARGIN_S = 5                # wake up this many seconds after the reset time
PAUSE_S = (0.1, 0.2)              # random pause before every request

LOG_FILE = Path("batch_run.log")  # one summary line per repository, appended run after run
STATE_DIR_NAME = ".state"         # checkpoint folder inside --out-dir
BATCH_STATE_FILE = "batch.state.json"
ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

KINDS = ("stars", "commits", "issues", "prs")   # order of work for each repository
KIND_LABELS = {                                 # middle part of the file names
    "stars": "stars_history",
    "commits": "commits_history",
    "issues": "issues_history",
    "prs": "pull_requests_history",
}

# Columns of every CSV file, in order.
STARS_COLUMNS = ["date", "stars_amount"]
STAR_DAYS_COLUMNS = ["date", "stars"]  # .partial only: new stars per day, newest first
STARGAZERS_COLUMNS = ["user", "timestamp", "cumulative_count"]
COMMITS_DAILY_COLUMNS = ["date", "commits_amount", "cumulative_commits"]
COMMITS_COLUMNS = [
    "sha", "url", "authored_date", "committed_date", "author_login", "author_name",
    "author_type", "committer_login", "message_headline", "additions", "deletions",
    "changed_files", "parents_count",
]
ISSUES_COLUMNS = [
    "opened_date", "issue_id", "url", "state", "state_reason", "title", "opened_by",
    "author_type", "labels", "comments_amount", "reactions_amount", "assignees",
    "milestone", "is_locked", "closed_at", "updated_at", "last_comment_date",
    "closing_links", "mentioned_links",
]
PRS_COLUMNS = [
    "opened_date", "pr_id", "url", "state", "is_draft", "title", "opened_by",
    "author_type", "user_role", "labels", "comments_amount", "reviews_count",
    "review_decision", "base_branch", "is_cross_repository", "additions", "deletions",
    "changed_files", "commits_count", "merged_by", "ci_status", "closed_at",
    "merged_at", "updated_at", "closing_links", "mentioned_links",
]
SUMMARY_COLUMNS = [
    "repo", "owner_type", "created_at", "pushed_at", "description", "primary_language",
    "languages", "license", "topics", "stars_amount", "forks_amount", "watchers_amount",
    "open_issues_amount", "open_prs_amount", "disk_usage_kb", "is_fork", "parent_repo",
    "is_archived", "default_branch",
]

# ---------------------------------------------------------------------------
# GraphQL queries (every query also asks for the current rate-limit status)
# ---------------------------------------------------------------------------

VIEWER_QUERY = """
query {
  rateLimit { cost remaining resetAt }
  viewer { login }
}
"""

REPO_BASICS_QUERY = """
query($owner: String!, $name: String!) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) { nameWithOwner createdAt stargazerCount }
}
"""

STARGAZERS_QUERY = """
query($owner: String!, $name: String!, $first: Int!, $after: String) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    stargazerCount
    stargazers(first: $first, after: $after, orderBy: {field: STARRED_AT, direction: ASC}) {
      totalCount
      pageInfo { endCursor hasNextPage }
      edges { starredAt node { login } }
    }
  }
}
"""

DEFAULT_BRANCH_QUERY = """
query($owner: String!, $name: String!) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    nameWithOwner
    defaultBranchRef { name target { oid } }
  }
}
"""

# History of one fixed commit: the head of the default branch when the fetch
# started. Commit-history cursors are relative to that commit, so pinning it
# keeps a saved cursor valid even if new commits are pushed before a resume.
COMMITS_QUERY = """
query($owner: String!, $name: String!, $oid: GitObjectID!, $first: Int!, $after: String) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    object(oid: $oid) {
      ... on Commit {
        history(first: $first, after: $after) {
          totalCount
          pageInfo { endCursor hasNextPage }
          nodes {
            oid url authoredDate committedDate messageHeadline
            additions deletions changedFilesIfAvailable
            parents { totalCount }
            author { name user { login __typename } }
            committer { user { login } }
          }
        }
      }
    }
  }
}
"""

# An issue or pull request referenced by another one ("Fixes #12", "see #12").
# Its URL gives both the repository and the number; asking for the nested
# `repository` object instead makes GitHub hit its resource limits.
LINK_FRAGMENT = """
fragment Link on ReferencedSubject {
  __typename
  ... on Issue { url }
  ... on PullRequest { url }
}
"""

# Nested connections are kept small so that a page of 100 issues or PRs stays
# within GitHub's limits; items with more labels or links than this get their
# remaining ones through NESTED_QUERY (see all_nodes).

# stateReason(enableDuplicate: true) returns DUPLICATE instead of folding it
# into NOT_PLANNED. includeClosedPrs: true also lists closed, unmerged PRs
# that declared they close the issue, as closingIssuesReferences does on the
# PR side.
ISSUES_QUERY = """
query($owner: String!, $name: String!, $first: Int!, $after: String) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    nameWithOwner
    issues(first: $first, after: $after, orderBy: {field: CREATED_AT, direction: ASC}) {
      totalCount
      pageInfo { endCursor hasNextPage }
      nodes {
        id number url title state createdAt closedAt updatedAt locked
        stateReason(enableDuplicate: true)
        author { login __typename }
        labels(first: 20) { pageInfo { endCursor hasNextPage } nodes { name } }
        assignees(first: 10) { pageInfo { endCursor hasNextPage } nodes { login } }
        milestone { title }
        comments(last: 1) { totalCount nodes { createdAt } }
        reactions { totalCount }
        closedByPullRequestsReferences(first: 10, includeClosedPrs: true) {
          pageInfo { endCursor hasNextPage }
          nodes { ...Link }
        }
        timelineItems(first: 20, itemTypes: [CROSS_REFERENCED_EVENT]) {
          pageInfo { endCursor hasNextPage }
          nodes { ... on CrossReferencedEvent { source { ...Link } } }
        }
      }
    }
  }
}
""" + LINK_FRAGMENT

# `comments` counts conversation comments only: review comments (written on
# lines of code inside a review) are not included.
PRS_QUERY = """
query($owner: String!, $name: String!, $first: Int!, $after: String) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    nameWithOwner
    pullRequests(first: $first, after: $after, orderBy: {field: CREATED_AT, direction: ASC}) {
      totalCount
      pageInfo { endCursor hasNextPage }
      nodes {
        id number url title state isDraft createdAt closedAt mergedAt updatedAt
        author { login __typename }
        authorAssociation
        labels(first: 20) { pageInfo { endCursor hasNextPage } nodes { name } }
        comments { totalCount }
        reviews { totalCount }
        reviewDecision
        baseRefName isCrossRepository additions deletions changedFiles
        commits(last: 1) { totalCount nodes { commit { statusCheckRollup { state } } } }
        mergedBy { login }
        closingIssuesReferences(first: 10) {
          pageInfo { endCursor hasNextPage }
          nodes { ...Link }
        }
        timelineItems(first: 20, itemTypes: [CROSS_REFERENCED_EVENT]) {
          pageInfo { endCursor hasNextPage }
          nodes { ... on CrossReferencedEvent { source { ...Link } } }
        }
      }
    }
  }
}
""" + LINK_FRAGMENT

# Further pages of a connection nested in one issue or PR, used only when its
# first 100 labels or links were not enough.
NESTED_QUERY = """
query($id: ID!, $first: Int!, $after: String) {
  rateLimit { cost remaining resetAt }
  node(id: $id) {
    ... on %(type)s {
      items: %(field)s { pageInfo { endCursor hasNextPage } nodes { %(nodes)s } }
    }
  }
}
"""
NESTED_FIELDS = {  # connection name -> (field with its arguments, selection of each node)
    "labels": ("labels(first: $first, after: $after)", "name"),
    "assignees": ("assignees(first: $first, after: $after)", "login"),
    "closedByPullRequestsReferences": (
        "closedByPullRequestsReferences(first: $first, after: $after, includeClosedPrs: true)",
        "...Link"),
    "closingIssuesReferences": ("closingIssuesReferences(first: $first, after: $after)", "...Link"),
    "timelineItems": (
        "timelineItems(first: $first, after: $after, itemTypes: [CROSS_REFERENCED_EVENT])",
        "... on CrossReferencedEvent { source { ...Link } }"),
}

SUMMARY_QUERY = """
query($owner: String!, $name: String!) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    nameWithOwner createdAt pushedAt description diskUsage isFork isArchived
    stargazerCount forkCount
    owner { __typename }
    primaryLanguage { name }
    languages(first: 100, orderBy: {field: SIZE, direction: DESC}) { edges { size node { name } } }
    licenseInfo { spdxId }
    repositoryTopics(first: 100) { nodes { topic { name } } }
    watchers { totalCount }
    openIssues: issues(states: OPEN) { totalCount }
    openPullRequests: pullRequests(states: OPEN) { totalCount }
    parent { nameWithOwner }
    defaultBranchRef { name }
  }
}
"""

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RepoError(Exception):
    """The repository cannot be fetched at all (not found, private, forbidden...).
    Not retried: the script logs it and moves on to the next repository."""


class AuthError(RepoError):
    """GitHub rejected the token (HTTP 401)."""


class FetchError(Exception):
    """One repository and kind could not be completed (e.g. retries exhausted).
    Its checkpoint is kept, so rerunning the command resumes it."""


class CursorRejected(Exception):
    """GitHub does not accept the saved pagination cursor any more."""


class StarListRestricted(Exception):
    """GitHub hides this repository's stargazer list from the token's account."""


# ---------------------------------------------------------------------------
# Console output and batch_run.log
# ---------------------------------------------------------------------------


class Console:
    """Terminal output: one progress line updated in place, plus normal messages."""

    def __init__(self) -> None:
        self.interactive = sys.stdout.isatty()
        self.active = False      # a progress line is currently on screen
        self.last_len = 0
        self.last_plain = time.monotonic()  # last progress print (non-terminal output)

    def progress(self, text: str, final: bool = False) -> None:
        """Show `text` on the progress line (printed at most every 30 s when
        the output is redirected to a file)."""
        if self.interactive:
            width = max(shutil.get_terminal_size((100, 20)).columns - 1, 20)
            text = text[:width]
            sys.stdout.write("\r" + text + " " * max(self.last_len - len(text), 0))
            sys.stdout.flush()
            self.active, self.last_len = True, len(text)
            if final:
                self.newline()
        elif final or time.monotonic() - self.last_plain >= 30:
            print(text, flush=True)
            self.last_plain = time.monotonic()

    def newline(self) -> None:
        """End the current progress line so that the next output starts below it."""
        if self.active:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.active, self.last_len = False, 0

    def message(self, text: str) -> None:
        """Print a message on its own line, below any progress line."""
        self.newline()
        print(text, flush=True)


CONSOLE = Console()


def log_line(text: str) -> None:
    """Append one timestamped line to batch_run.log."""
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"{utc_now_iso()} {text}\n")


def log_event(text: str) -> None:
    """Show a message and also append it to batch_run.log."""
    CONSOLE.message(text)
    log_line(text.strip())


@contextmanager
def critical_section() -> Iterator[None]:
    """Postpone Ctrl+C until the block has finished, so that a page of rows
    and the state file describing it are always written together."""
    pending: List[int] = []
    try:
        previous = signal.signal(signal.SIGINT, lambda signum, frame: pending.append(signum))
    except ValueError:  # signals can only be handled in the main thread
        previous = None
    try:
        yield
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)
        if pending:
            raise KeyboardInterrupt


# ---------------------------------------------------------------------------
# Small helpers: time, formatting, files
# ---------------------------------------------------------------------------


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def utc_now_iso() -> str:
    return utc_now().strftime(ISO_FORMAT)


def parse_timestamp(value: str) -> dt.datetime:
    """Parse an ISO 8601 timestamp into an aware UTC datetime
    (Python 3.9's fromisoformat does not understand a trailing 'Z')."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def iso_utc(value: Optional[str]) -> str:
    """Full ISO 8601 UTC timestamp ('2024-05-01T12:30:00Z'), or '' when missing."""
    return parse_timestamp(value).strftime(ISO_FORMAT) if value else ""


def date_only(value: Optional[str]) -> str:
    """Calendar date (UTC) of a timestamp as YYYY-MM-DD, or '' when missing."""
    return parse_timestamp(value).strftime("%Y-%m-%d") if value else ""


def week_start(day: dt.date) -> dt.date:
    """The Sunday on or before `day` (star history weeks start on Sunday)."""
    return day - dt.timedelta(days=(day.weekday() + 1) % 7)


def fmt_duration(seconds: float) -> str:
    """Human-readable duration: '45 s', '12 min 05 s', '2 h 03 min'."""
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {secs:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"


def plural(count: int, word: str, words: Optional[str] = None) -> str:
    """'1 repository', '2 repositories'."""
    return f"{count:,} {word if count == 1 else (words or word + 's')}"


def split_repo(repo: str) -> Tuple[str, str]:
    owner, name = repo.split("/", 1)
    return owner, name


def cell(value: Any) -> Any:
    """Convert a Python value to its CSV form: None -> '', booleans -> true/false,
    lists -> semicolon-separated text."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ";".join(str(item) for item in value)
    return value


def write_csv(path: Path, columns: Sequence[str], rows: Sequence[Dict[str, Any]],
              append: bool = True) -> int:
    """Write rows to a UTF-8 CSV file and return the file's new size in bytes.

    With append=True the rows are added at the end and the header is written
    only if the file is new; with append=False the file is replaced. Fields
    containing commas, quotes or line breaks are quoted, so titles cannot
    break the file.
    """
    new_file = not append or not path.exists() or path.stat().st_size == 0
    with path.open("a" if append else "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        if new_file:
            writer.writerow(columns)
        for row in rows:
            writer.writerow([cell(row.get(column)) for column in columns])
        handle.flush()
        os.fsync(handle.fileno())
    return path.stat().st_size


def replace_file(source: Path, target: Path) -> None:
    """Atomically move `source` over `target` (retrying briefly when Windows
    reports the target as busy, e.g. because it is open in Excel)."""
    for attempt in range(5):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 4:
                raise FetchError(f"cannot write {target.name}: is it open in another program?") from None
            time.sleep(1)


def write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    """Write JSON to a temporary file, then rename it into place, so the file
    is never left half-written."""
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    replace_file(temporary, path)


def write_frame_atomic(path: Path, frame: pd.DataFrame) -> None:
    """Write a DataFrame as CSV to a temporary file, then rename it into place."""
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        frame.to_csv(handle, index=False, lineterminator="\n")
        handle.flush()
        os.fsync(handle.fileno())
    replace_file(temporary, path)


def daily_frame(counts: pd.Series, start: str, end: str) -> pd.DataFrame:
    """One row per calendar day from `start` to `end` (inclusive) with the
    count of that day (0 when missing) and the running total."""
    days = pd.date_range(start=start, end=end, freq="D").strftime("%Y-%m-%d")
    per_day = counts.reindex(days, fill_value=0).astype("int64")
    return pd.DataFrame({
        "date": list(days),
        "count": per_day.to_numpy(),
        "cumulative": per_day.cumsum().to_numpy(),
    })


# ---------------------------------------------------------------------------
# GitHub client: one request at a time, rate limits, retries
# ---------------------------------------------------------------------------


class PageSize:
    """Page size of one fetch. Starts at 100 and steps down to 50, 25, 10 when
    GitHub times out or reports a node or secondary rate limit."""

    def __init__(self, value: Optional[int] = None) -> None:
        self.value = value if value in PAGE_SIZES else PAGE_SIZES[0]

    def reduce(self) -> bool:
        smaller = [size for size in PAGE_SIZES if size < self.value]
        if not smaller:
            return False
        self.value = smaller[0]
        return True


@dataclass
class RetryState:
    """Failures seen so far while trying to complete one request."""
    attempts: int = 0          # transient errors (timeouts, resets, 5xx)
    secondary_waits: int = 0   # secondary rate-limit waits


def _response_message(response: requests.Response) -> str:
    """The error message GitHub sent, if any."""
    try:
        return str(response.json().get("message", ""))[:300]
    except ValueError:
        return response.text[:200].strip()


def _looks_like_timeout(error: Dict[str, Any]) -> bool:
    message = str(error.get("message", "")).lower()
    return ("timeout" in message or "timed out" in message
            or "something went wrong while executing your query" in message
            or error.get("type") in ("SERVICE_UNAVAILABLE", "TIMEOUT", "INTERNAL"))


def _is_star_list_error(error: Dict[str, Any]) -> bool:
    return error.get("type") == "FORBIDDEN" and "stargazers" in (error.get("path") or [])


class GitHubClient:
    """Sends requests to GitHub strictly one at a time and handles rate limits,
    retries and page-size reduction."""

    def __init__(self, token: str) -> None:
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"bearer {token}", "User-Agent": USER_AGENT})
        # GraphQL and REST ("core") requests have separate hourly quotas.
        self.limits: Dict[str, Dict[str, Any]] = {"graphql": {}, "core": {}}
        self.points_used = 0  # GraphQL points spent during this run

    # -- public methods ------------------------------------------------------

    def graphql(self, query: str, variables: Optional[Dict[str, Any]] = None,
                page_size: Optional[PageSize] = None) -> Dict[str, Any]:
        """Run one GraphQL query and return its "data" object.

        When `page_size` is given its value is sent as the `first` variable and
        lowered automatically if GitHub times out or reports a node limit.
        Raises RepoError, FetchError or CursorRejected when retrying cannot help.
        """
        variables = dict(variables or {})
        retry = RetryState()
        while True:
            if page_size is not None:
                variables["first"] = page_size.value
            response = self._send("graphql", "POST", GRAPHQL_URL, retry, page_size,
                                  json={"query": query, "variables": variables})
            if response is None:
                continue  # a retryable problem was handled (waited); try again
            try:
                payload = response.json()
            except ValueError:
                self._transient(retry, "GitHub sent an unreadable response", page_size)
                continue
            data = payload.get("data") if isinstance(payload, dict) else None
            rate = data.get("rateLimit") if isinstance(data, dict) else None
            if rate:
                self.points_used += int(rate.get("cost") or 0)
                if rate.get("remaining") is not None:
                    self.limits["graphql"]["remaining"] = int(rate["remaining"])
                if rate.get("resetAt"):
                    self.limits["graphql"]["reset_at"] = parse_timestamp(rate["resetAt"])
            errors = payload.get("errors") if isinstance(payload, dict) else None
            if not errors:
                if not isinstance(data, dict):
                    self._transient(retry, "GitHub sent an empty response", page_size)
                    continue
                return data
            if self._handle_graphql_errors(errors, data, retry, page_size):
                continue
            return data

    def rest_get(self, path: str, params: Dict[str, Any]) -> Any:
        """GET a REST endpoint (used only for the daily star history) and
        return its JSON, with the same retry and rate-limit rules."""
        retry = RetryState()
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": REST_API_VERSION}
        while True:
            response = self._send("core", "GET", REST_URL + path, retry, None,
                                  params=params, headers=headers)
            if response is None:
                continue
            try:
                return response.json()
            except ValueError:
                self._transient(retry, "GitHub sent an unreadable response", None)

    def check_token(self) -> Tuple[str, Optional[int]]:
        """Validate the token with a cheap query; return (login, points left)."""
        data = self.graphql(VIEWER_QUERY)
        return data["viewer"]["login"], self.limits["graphql"].get("remaining")

    # -- internals -----------------------------------------------------------

    def _send(self, quota: str, method: str, url: str, retry: RetryState,
              page_size: Optional[PageSize], **kwargs: Any) -> Optional[requests.Response]:
        """Send one HTTP request.

        Returns the response if GitHub answered HTTP 200, None after waiting out
        a retryable problem, and raises when retrying cannot help.
        """
        self._wait_if_quota_low(quota)
        time.sleep(random.uniform(*PAUSE_S))  # be gentle: short pause between requests
        try:
            response = self.session.request(method, url, timeout=REQUEST_TIMEOUT_S, **kwargs)
        except requests.ReadTimeout:
            # The query ran for more than 60 s: retry with a smaller page.
            self._transient(retry, f"no answer within {REQUEST_TIMEOUT_S} s", page_size, shrink=True)
            return None
        except requests.RequestException as exc:  # connection reset, DNS failure, ...
            self._transient(retry, f"connection problem ({type(exc).__name__})", page_size)
            return None
        self._read_rate_headers(quota, response)
        status = response.status_code
        if status == 200:
            return response
        message = _response_message(response)
        if status == 401:
            raise AuthError("GitHub rejected the token (HTTP 401 Bad credentials)")
        if status in (403, 429):
            retry_after = response.headers.get("retry-after")
            if not retry_after and response.headers.get("x-ratelimit-remaining") == "0":
                self._sleep_until_reset(quota, "the hourly rate limit is used up")
                return None
            lowered = message.lower()
            if retry_after or status == 429 or "secondary rate limit" in lowered or "abuse" in lowered:
                self._secondary_wait(retry, retry_after, page_size)
                return None
            raise RepoError(f"access denied (HTTP {status}): {message}")
        if status in (502, 504):
            self._transient(retry, f"GitHub timed out (HTTP {status})", page_size, shrink=True)
            return None
        if status >= 500:
            self._transient(retry, f"GitHub server error (HTTP {status})", page_size)
            return None
        if status == 422 and quota == "core":
            # The star history endpoint answers 422 when it "has been spammed".
            self._transient(retry, "GitHub refused the request for now (HTTP 422)", page_size)
            return None
        if status == 451:
            raise RepoError("repository unavailable for legal reasons (HTTP 451)")
        raise FetchError(f"unexpected answer from GitHub (HTTP {status}): {message}")

    def _handle_graphql_errors(self, errors: List[Dict[str, Any]], data: Any,
                               retry: RetryState, page_size: Optional[PageSize]) -> bool:
        """React to the "errors" list of a GraphQL answer.

        Returns True to retry the request, False to use the (partial) data, or
        raises when the request cannot succeed.
        """
        types = {str(error.get("type", "")) for error in errors}
        text = "; ".join(str(error.get("message", "")) for error in errors)[:400]
        if "RATE_LIMITED" in types:
            self._sleep_until_reset("graphql", "the hourly GraphQL rate limit is used up")
            return True
        if any(e.get("type") == "NOT_FOUND" and e.get("path") == ["repository"] for e in errors):
            raise RepoError("repository not found (it may be private, renamed or deleted)")
        if "INVALID_CURSOR_ARGUMENTS" in types:
            raise CursorRejected(text)
        if types & {"MAX_NODE_LIMIT_EXCEEDED", "RESOURCE_LIMITS_EXCEEDED"} or "node limit" in text.lower():
            # GitHub drops (parts of) the data: never keep it, ask for smaller pages.
            if page_size is not None and page_size.reduce():
                CONSOLE.message(f"  Query too large for GitHub; retrying with page size {page_size.value}")
                return True
            self._transient(retry, "GitHub's resource limits were exceeded", page_size)
            return True
        if any(_looks_like_timeout(error) for error in errors):
            self._transient(retry, "GitHub could not finish the query in time", page_size, shrink=True)
            return True
        if any(e.get("type") == "FORBIDDEN" and e.get("path") == ["repository"] for e in errors):
            raise RepoError(f"access to the repository is forbidden: {text}")
        usable = isinstance(data, dict) and any(
            value is not None for key, value in data.items() if key != "rateLimit")
        if not usable:
            raise FetchError(f"GitHub returned an error: {text}")
        # GitHub answered but reported problems with part of the data.
        if not all(_is_star_list_error(error) for error in errors):
            log_event(f"  WARNING: GitHub reported errors for part of the data: {text}")
        return False

    def _transient(self, retry: RetryState, reason: str, page_size: Optional[PageSize],
                   shrink: bool = False) -> None:
        """Wait before retrying a transient failure (2, 4, 8, 16, 32 s), lowering
        the page size if the query was too slow; give up after MAX_ATTEMPTS."""
        retry.attempts += 1
        if retry.attempts >= MAX_ATTEMPTS:
            raise FetchError(f"{reason}; gave up after {MAX_ATTEMPTS} attempts")
        wait = 2 ** retry.attempts
        note = ""
        if shrink and page_size is not None and page_size.reduce():
            note = f", page size lowered to {page_size.value}"
        CONSOLE.message(f"  {reason}; retrying in {wait} s "
                        f"(attempt {retry.attempts + 1} of {MAX_ATTEMPTS}{note})")
        time.sleep(wait)

    def _secondary_wait(self, retry: RetryState, retry_after: Optional[str],
                        page_size: Optional[PageSize]) -> None:
        """Secondary rate limit: honour Retry-After, otherwise wait 60, 120,
        240 s ... (at most 15 min), and use smaller pages from now on."""
        retry.secondary_waits += 1
        if retry.secondary_waits > MAX_SECONDARY_WAITS:
            raise FetchError("GitHub's secondary rate limit kept blocking requests")
        if retry_after and retry_after.strip().isdigit():
            wait = int(retry_after.strip())
        else:
            wait = min(SECONDARY_WAIT_S * 2 ** (retry.secondary_waits - 1), SECONDARY_WAIT_MAX_S)
        note = ""
        if page_size is not None and page_size.reduce():
            note = f"; page size lowered to {page_size.value}"
        log_event(f"  Secondary rate limit reached; waiting {fmt_duration(wait)} before retrying{note}.")
        time.sleep(wait)

    def _read_rate_headers(self, quota: str, response: requests.Response) -> None:
        """Remember the quota left and the reset time sent in the HTTP headers."""
        remaining = response.headers.get("x-ratelimit-remaining", "")
        reset = response.headers.get("x-ratelimit-reset", "")
        if remaining.isdigit():
            self.limits[quota]["remaining"] = int(remaining)
        if reset.isdigit():
            self.limits[quota]["reset_at"] = dt.datetime.fromtimestamp(int(reset), dt.timezone.utc)

    def _wait_if_quota_low(self, quota: str) -> None:
        """Before a request: if fewer than RATE_LIMIT_FLOOR points/requests are
        left in this hour's quota, sleep until it resets."""
        info = self.limits[quota]
        remaining, reset_at = info.get("remaining"), info.get("reset_at")
        if remaining is None or remaining >= RATE_LIMIT_FLOOR or reset_at is None:
            return
        if reset_at <= utc_now():
            return  # the quota has already been renewed
        unit = "points" if quota == "graphql" else "REST requests"
        self._sleep_until_reset(quota, f"only {remaining} {unit} left this hour")
        info["remaining"] = None  # unknown until the next answer

    def _sleep_until_reset(self, quota: str, reason: str) -> None:
        """Sleep until the hourly quota resets (plus a few seconds)."""
        now = utc_now()
        reset_at = self.limits[quota].get("reset_at")
        if reset_at is None or reset_at <= now:
            reset_at = now + dt.timedelta(seconds=60)
        wake = reset_at + dt.timedelta(seconds=RESET_MARGIN_S)
        seconds = (wake - now).total_seconds()
        log_event(f"  Rate limit: {reason}. Sleeping until {wake.astimezone():%H:%M:%S} local time "
                  f"({wake:%H:%M:%S} UTC), about {fmt_duration(seconds)}. Progress is saved: you can "
                  f"also stop with Ctrl+C and rerun the same command later.")
        time.sleep(seconds)


# ---------------------------------------------------------------------------
# Checkpointed fetches
# ---------------------------------------------------------------------------


@dataclass
class RunContext:
    """Settings and counters shared by the whole run."""
    out_dir: Path
    state_dir: Path
    date: str                    # collection date of this batch (YYYY-MM-DD, UTC)
    batch: Dict[str, Any]        # contents of .state/batch.state.json
    failures: int = 0            # FAILED lines logged during this run
    pending: int = 0             # failures that left a checkpoint to resume
    fatal_repos: Set[str] = field(default_factory=set)
    renamed: Set[str] = field(default_factory=set)


@dataclass
class Dataset:
    """One checkpointed download: per-item rows go to a .partial file and
    progress to a .state.json file until the final CSV files are written."""
    repo: str              # owner/repo as written in the list
    kind: str              # stars, commits, issues or prs
    name: str              # identifies the dataset inside its state file
    label: str             # shown on the progress line
    unit: str              # what one row is ("stars", "commits", "days" ...)
    columns: List[str]     # header of the .partial file
    partial: Path          # rows fetched so far
    state_path: Path       # checkpoint
    outputs: List[Path]    # final CSV files, present only once the fetch is complete
    date: str              # collection date used in the file names


@dataclass
class Page:
    """One page of results, already converted to CSV rows."""
    rows: List[Dict[str, Any]]
    end_cursor: Optional[str]
    has_next: bool
    total: Optional[int] = None                           # total items, for the progress line
    extra: Dict[str, Any] = field(default_factory=dict)   # values to keep in the state file


PageFetcher = Callable[[Optional[str], PageSize, Dict[str, Any]], Page]


def file_stem(repo: str, kind: str, date: str) -> str:
    """'[user]-[repo]_<kind>_[date]', the start of every file name of a dataset."""
    owner, name = split_repo(repo)
    return f"{owner}-{name}_{KIND_LABELS[kind]}_{date}"


def find_checkpoint_date(ctx: RunContext, repo: str, kind: str, tail: str) -> Optional[str]:
    """Collection date of an unfinished fetch of this dataset, if one exists."""
    owner, name = split_repo(repo)
    pattern = re.compile(re.escape(f"{owner}-{name}_{KIND_LABELS[kind]}_")
                         + r"(\d{4}-\d{2}-\d{2})" + re.escape(tail) + r"\.state\.json$")
    dates = sorted(match.group(1) for match in
                   (pattern.match(path.name) for path in ctx.state_dir.glob("*.state.json")) if match)
    return dates[-1] if dates else None


def make_dataset(ctx: RunContext, repo: str, kind: str, *, label: str, unit: str,
                 columns: List[str], tail: str, partial_tail: str,
                 output_tails: Sequence[str]) -> Dataset:
    """Build the file names of one dataset. An unfinished fetch keeps the
    collection date it started with, so a dataset never mixes two dates."""
    date = find_checkpoint_date(ctx, repo, kind, tail) or ctx.date
    if date != ctx.date:
        log_event(f"  {repo} {label}: continuing a fetch started on {date}; "
                  f"its files keep the original date {date}")
    stem = file_stem(repo, kind, date)
    return Dataset(
        repo=repo, kind=kind, name=f"{kind}{tail}", label=label, unit=unit, columns=columns,
        partial=ctx.out_dir / f"{stem}{partial_tail}.csv.partial",
        state_path=ctx.state_dir / f"{stem}{tail}.state.json",
        outputs=[ctx.out_dir / f"{stem}{output_tail}.csv" for output_tail in output_tails],
        date=date,
    )


def outputs_complete(ctx: RunContext, ds: Dataset) -> bool:
    """True if all final files exist (and, in a batch started with --force,
    were rewritten by that batch)."""
    if not all(path.exists() for path in ds.outputs):
        return False
    if ds.kind in ctx.batch.get("force_kinds", []):
        started = float(ctx.batch.get("started_ts", 0)) - 2
        return all(path.stat().st_mtime >= started for path in ds.outputs)
    return True


def new_checkpoint(ds: Dataset) -> Dict[str, Any]:
    """State of a fetch that starts from scratch. Leftover files are deleted;
    the .partial and .state.json files are created when the first page is
    saved, so a repository that fails at once leaves nothing behind."""
    remove_checkpoint(ds)
    now = utc_now_iso()
    return {
        "repo": ds.repo, "dataset": ds.name, "collection_date": ds.date,
        "cursor": None,              # endCursor of the last saved page
        "items_fetched": 0,          # rows saved in the .partial file
        "complete": False,           # True once the last page has been saved
        "partial_file": ds.partial.name,
        "partial_bytes": 0,          # size of the .partial file at the last checkpoint
        "page_size": PAGE_SIZES[0],
        "total": None,
        "extra": {},
        "started_at": now,
        "updated_at": now,
    }


def load_checkpoint(ds: Dataset) -> Optional[Dict[str, Any]]:
    """Return the saved state if the fetch can be resumed, else None."""
    if not ds.state_path.exists():
        return None
    try:
        state = json.loads(ds.state_path.read_text(encoding="utf-8"))
        saved_bytes = int(state["partial_bytes"])
        state["items_fetched"] = int(state["items_fetched"])
        state.setdefault("extra", {})
        state.setdefault("complete", False)
    except (OSError, ValueError, KeyError, TypeError):
        log_event(f"  {ds.repo} {ds.label}: the state file is unreadable; starting this fetch again")
        return None
    if state.get("repo") != ds.repo or state.get("dataset") != ds.name:
        log_event(f"  {ds.repo} {ds.label}: the state file belongs to another dataset; starting again")
        return None
    if not ds.partial.exists():
        log_event(f"  {ds.repo} {ds.label}: {ds.partial.name} is missing; starting this fetch again")
        return None
    actual = ds.partial.stat().st_size
    if actual < saved_bytes:
        log_event(f"  {ds.repo} {ds.label}: {ds.partial.name} is shorter than expected; starting again")
        return None
    if actual > saved_bytes:
        # Rows written after the last checkpoint (e.g. the process was killed
        # between the two writes): drop them, they are fetched again.
        with ds.partial.open("r+b") as handle:
            handle.truncate(saved_bytes)
    return state


def remove_checkpoint(ds: Dataset) -> None:
    """Delete the .partial and .state.json files of a dataset (and temporary
    files a crash may have left behind)."""
    leftovers = [ds.state_path, ds.partial, ds.state_path.with_name(ds.state_path.name + ".tmp")]
    leftovers += [path.with_name(path.name + ".tmp") for path in ds.outputs]
    for path in leftovers:
        path.unlink(missing_ok=True)


def show_progress(ds: Dataset, state: Dict[str, Any], final: bool = False) -> None:
    """Update the progress line: items fetched / total."""
    done, total = state["items_fetched"], state.get("total")
    if done == 0 and not final and not CONSOLE.interactive:
        return  # nothing worth a line in a log file yet
    text = f"  {ds.label}: {done:,}"
    if total:
        text += f"/{total:,} {ds.unit} ({min(done * 100 // total, 100)}%)"
    else:
        text += f" {ds.unit}"
    if final:
        text += " - saved"
    CONSOLE.progress(text, final=final)


def run_dataset(ctx: RunContext, ds: Dataset, fetch_page: PageFetcher,
                finalize: Callable[[Dataset, Dict[str, Any]], None]) -> str:
    """Download every page of one dataset, checkpointing after each page.

    Returns a short outcome for batch_run.log. Raises FetchError or RepoError
    on failure (the checkpoint is kept) and KeyboardInterrupt on Ctrl+C.
    """
    if outputs_complete(ctx, ds):
        remove_checkpoint(ds)  # leftovers of a fetch that had already finished
        CONSOLE.message(f"  {ds.label}: already saved for {ds.date}, skipped")
        return "skipped (already saved)"
    state = load_checkpoint(ds)
    if state is None:
        state = new_checkpoint(ds)
    else:
        log_event(f"  {ds.repo} {ds.label}: resuming from the saved cursor "
                  f"({state['items_fetched']:,} {ds.unit} already saved, collection date {ds.date})")
    page_size = PageSize(state.get("page_size"))
    restarted = False
    show_progress(ds, state)
    while not state["complete"]:
        try:
            page = fetch_page(state["cursor"], page_size, state)
        except RepoError as exc:
            if not isinstance(exc, AuthError):
                remove_checkpoint(ds)  # the repository cannot be fetched: nothing to resume
            raise
        except CursorRejected as exc:
            if restarted or state["cursor"] is None:
                raise FetchError(f"GitHub rejected the pagination cursor: {exc}") from None
            log_event(f"  {ds.repo} {ds.label}: GitHub rejected the saved cursor; "
                      f"restarting this fetch from the beginning")
            state, page_size, restarted = new_checkpoint(ds), PageSize(), True
            continue
        with critical_section():  # rows and state are saved together
            state["partial_bytes"] = write_csv(ds.partial, ds.columns, page.rows)
            state["items_fetched"] += len(page.rows)
            if page.end_cursor:
                state["cursor"] = page.end_cursor
            state["complete"] = not page.has_next
            state["page_size"] = page_size.value
            if page.total is not None:
                state["total"] = page.total
            state["extra"].update(page.extra)
            state["updated_at"] = utc_now_iso()
            write_json_atomic(ds.state_path, state)
        show_progress(ds, state)
    with critical_section():
        finalize(ds, state)
        remove_checkpoint(ds)
    show_progress(ds, state, final=True)
    return f"OK ({state['items_fetched']:,} {ds.unit})"


def finalize_rename(ds: Dataset, state: Dict[str, Any]) -> None:
    """Per-item files: the complete .partial file becomes the final CSV."""
    replace_file(ds.partial, ds.outputs[0])


def note_rename(ctx: RunContext, repo: str, canonical: Optional[str]) -> None:
    """Log once when GitHub redirects a listed repository to another name."""
    if canonical and canonical.lower() != repo.lower() and repo not in ctx.renamed:
        ctx.renamed.add(repo)
        log_event(f"  NOTE: {repo} redirects to {canonical} (renamed or transferred); "
                  f"files keep the name used in the list")


# ---------------------------------------------------------------------------
# 1. Stars
# ---------------------------------------------------------------------------


def fetch_stars(client: GitHubClient, ctx: RunContext, repo: str) -> str:
    """Stars of one repository.

    1. "[user]-[repo]_stars_history_[date].csv": cumulative stars per day, built
       from GitHub's star history endpoint (REST; daily counts grouped by week).
       Since July 2026 this is the only star history GitHub gives for
       repositories you do not administer, and it has no GraphQL equivalent.
    2. "..._complete_history.csv": one row per star, from GraphQL `stargazers`
       ordered by STARRED_AT ASC. GitHub returns this list only to the
       repository's admins and collaborators; otherwise the file is skipped
       and the log says why.
    """
    owner, name = split_repo(repo)
    today = utc_now().date()

    daily = make_dataset(ctx, repo, "stars", label="stars (daily)", unit="days",
                         columns=STAR_DAYS_COLUMNS, tail="", partial_tail="", output_tails=("",))

    def star_days_page(cursor: Optional[str], page_size: PageSize, state: Dict[str, Any]) -> Page:
        known, extra = state["extra"], {}
        created_at = known.get("created_at")
        if created_at is None:  # first page: also check that the repository exists
            node = client.graphql(REPO_BASICS_QUERY, {"owner": owner, "name": name})["repository"]
            note_rename(ctx, repo, node.get("nameWithOwner"))
            created_at = node["createdAt"]
            extra = {"created_at": created_at, "stargazer_count": node["stargazerCount"]}
        created = parse_timestamp(created_at).date()
        page_number = int(cursor or 0) + 1  # the "cursor" is the last page number saved
        weeks = client.rest_get(f"/repos/{owner}/{name}/stargazers/history",
                                {"per_page": STAR_HISTORY_PAGE_SIZE, "page": page_number})
        if not isinstance(weeks, list):
            raise FetchError("unexpected answer from the star history endpoint")
        oldest_saved = known.get("oldest_day")
        rows: List[Dict[str, Any]] = []
        oldest_week: Optional[dt.date] = None
        for week in weeks:  # newest week first; "days" starts on Sunday
            first_day = dt.datetime.fromtimestamp(int(week["week"]), dt.timezone.utc).date()
            oldest_week = first_day if oldest_week is None else min(oldest_week, first_day)
            for offset, count in enumerate(week.get("days") or []):
                day = (first_day + dt.timedelta(days=offset)).isoformat()
                # Pages are counted back from the current week, so they shift
                # when a new week starts: skip days a previous page already saved.
                if oldest_saved is None or day < oldest_saved:
                    rows.append({"date": day, "stars": int(count)})
        if rows:
            extra["oldest_day"] = min(row["date"] for row in rows)
        reached_creation = oldest_week is not None and oldest_week <= created
        has_next = (len(weeks) == STAR_HISTORY_PAGE_SIZE and not reached_creation
                    and page_number < STAR_HISTORY_MAX_PAGE)
        total_days = (week_start(today) - week_start(created)).days + 7
        return Page(rows, str(page_number), has_next, total_days, extra)

    daily_outcome = run_dataset(ctx, daily, star_days_page, finalize_star_days)

    per_star = make_dataset(ctx, repo, "stars", label="stars (per star)", unit="stars",
                            columns=STARGAZERS_COLUMNS, tail="_complete_history",
                            partial_tail="_complete_history", output_tails=("_complete_history",))

    def stargazers_page(cursor: Optional[str], page_size: PageSize, state: Dict[str, Any]) -> Page:
        node = client.graphql(STARGAZERS_QUERY, {"owner": owner, "name": name, "after": cursor},
                              page_size)["repository"]
        stargazers = node.get("stargazers")
        # Without access GitHub answers with an empty list (or an error)
        # although the repository has stars.
        if stargazers is None or (stargazers["totalCount"] == 0 and node["stargazerCount"] > 0):
            raise StarListRestricted()
        count = state["items_fetched"]
        rows = []
        for edge in stargazers.get("edges") or []:
            if not edge:
                continue
            count += 1
            rows.append({
                "user": (edge.get("node") or {}).get("login") or "ghost",
                "timestamp": iso_utc(edge.get("starredAt")),
                "cumulative_count": count,
            })
        info = stargazers["pageInfo"]
        return Page(rows, info["endCursor"], info["hasNextPage"], stargazers["totalCount"])

    try:
        per_star_outcome = run_dataset(ctx, per_star, stargazers_page, finalize_rename)
    except StarListRestricted:
        remove_checkpoint(per_star)
        CONSOLE.message("  stars (per star): not available - GitHub shows stargazer lists "
                        "only to repository admins and collaborators")
        per_star_outcome = "not available (GitHub restricts stargazer lists)"
    return f"daily {daily_outcome}, per-star {per_star_outcome}"


def finalize_star_days(ds: Dataset, state: Dict[str, Any]) -> None:
    """Daily stars file: cumulative stars on every calendar day from the
    repository's creation (or first star) to the collection date."""
    raw = pd.read_csv(ds.partial, dtype={"date": str, "stars": "int64"}, keep_default_na=False)
    counts = raw.groupby("date")["stars"].sum()
    active = counts[counts > 0]
    created = date_only(state["extra"].get("created_at")) or ds.date
    start = min([created] + list(active.index[:1]))
    end = max([ds.date] + list(active.index[-1:]))  # later if stars arrived after midnight
    frame = daily_frame(counts, start, end).rename(columns={"cumulative": "stars_amount"})
    write_frame_atomic(ds.outputs[0], frame[STARS_COLUMNS])
    expected, total = state["extra"].get("stargazer_count"), int(counts.sum())
    if expected is not None and total != expected:
        log_event(f"  NOTE: {ds.repo}: the daily star history adds up to {total:,} stars while "
                  f"GitHub reported {expected:,} when the fetch started (stars changed meanwhile?)")


# ---------------------------------------------------------------------------
# 2. Commits
# ---------------------------------------------------------------------------


def commit_row(node: Dict[str, Any]) -> Dict[str, Any]:
    """One CSV row for a commit (no e-mail addresses are collected)."""
    author = node.get("author") or {}
    author_user = author.get("user") or {}  # empty when not linked to a GitHub account
    committer_user = (node.get("committer") or {}).get("user") or {}
    return {
        "sha": node.get("oid"),
        "url": node.get("url"),
        "authored_date": iso_utc(node.get("authoredDate")),
        "committed_date": iso_utc(node.get("committedDate")),
        "author_login": author_user.get("login", ""),
        "author_name": author.get("name") or "",
        "author_type": author_user.get("__typename", ""),
        "committer_login": committer_user.get("login", ""),
        "message_headline": node.get("messageHeadline") or "",
        "additions": node.get("additions"),
        "deletions": node.get("deletions"),
        "changed_files": node.get("changedFilesIfAvailable"),
        "parents_count": (node.get("parents") or {}).get("totalCount"),
    }


def fetch_commits(client: GitHubClient, ctx: RunContext, repo: str) -> str:
    """Commits of the default branch only: one row per commit, plus commits per day."""
    owner, name = split_repo(repo)
    ds = make_dataset(ctx, repo, "commits", label="commits", unit="commits",
                      columns=COMMITS_COLUMNS, tail="", partial_tail="_complete_history",
                      output_tails=("", "_complete_history"))

    def commits_page(cursor: Optional[str], page_size: PageSize, state: Dict[str, Any]) -> Page:
        extra: Dict[str, Any] = {}
        head = state["extra"].get("head_oid")
        if head is None:  # first page: pin the current head of the default branch
            node = client.graphql(DEFAULT_BRANCH_QUERY, {"owner": owner, "name": name})["repository"]
            note_rename(ctx, repo, node.get("nameWithOwner"))
            ref = node.get("defaultBranchRef")
            if not ref or not ref.get("target"):
                return Page([], None, False, 0)  # empty repository: no commits at all
            head = ref["target"]["oid"]
            extra = {"head_oid": head, "branch": ref["name"]}
        data = client.graphql(COMMITS_QUERY, {"owner": owner, "name": name, "oid": head,
                                              "after": cursor}, page_size)
        target = data["repository"].get("object")
        if not target or "history" not in target:
            raise CursorRejected("the commit this fetch started from is no longer available")
        history = target["history"]
        rows = [commit_row(node) for node in history.get("nodes") or [] if node]
        info = history["pageInfo"]
        return Page(rows, info["endCursor"], info["hasNextPage"], history["totalCount"], extra)

    return run_dataset(ctx, ds, commits_page, finalize_commits)


def finalize_commits(ds: Dataset, state: Dict[str, Any]) -> None:
    """Commits per calendar day (by committed date, UTC) from the first commit
    to the collection date, then publish the per-commit file. The per-commit
    data is read in chunks so memory use stays flat for huge histories."""
    counts = pd.Series(dtype="float64")
    with pd.read_csv(ds.partial, usecols=["committed_date"], dtype=str,
                     keep_default_na=False, chunksize=100_000) as reader:
        for chunk in reader:
            days = chunk["committed_date"].str.slice(0, 10)
            counts = counts.add(days[days != ""].value_counts(), fill_value=0)
    daily_path, complete_path = ds.outputs
    if counts.empty:
        frame = pd.DataFrame(columns=COMMITS_DAILY_COLUMNS)
    else:
        counts = counts.sort_index()
        frame = daily_frame(counts, counts.index[0], max(ds.date, counts.index[-1]))
        frame.columns = COMMITS_DAILY_COLUMNS
    write_frame_atomic(daily_path, frame)
    replace_file(ds.partial, complete_path)


# ---------------------------------------------------------------------------
# 3. Issues and 4. pull requests
# ---------------------------------------------------------------------------


def all_nodes(client: GitHubClient, item: Dict[str, Any], connection: str,
              type_name: str) -> List[Dict[str, Any]]:
    """All nodes of a connection nested in an issue or PR (labels, links ...),
    fetching further pages when the first 100 were not enough."""
    conn = item.get(connection) or {}
    nodes = [node for node in conn.get("nodes") or [] if node]
    info = conn.get("pageInfo") or {}
    if info.get("hasNextPage"):
        call, selection = NESTED_FIELDS[connection]
        query = NESTED_QUERY % {"type": type_name, "field": call, "nodes": selection}
        if "...Link" in selection:
            query += LINK_FRAGMENT
        while info.get("hasNextPage"):
            try:
                data = client.graphql(query, {"id": item["id"], "after": info.get("endCursor")},
                                      PageSize())
            except CursorRejected as exc:  # not the main cursor: do not restart the dataset
                raise FetchError(f"could not page through {connection}: {exc}") from None
            sub = (data.get("node") or {}).get("items") or {}
            nodes.extend(node for node in sub.get("nodes") or [] if node)
            info = sub.get("pageInfo") or {}
    return nodes


def _ref_sort_key(ref: str) -> Tuple[int, str, int]:
    """Same-repository numbers first (numerically), then other repositories."""
    if ref.isdigit():
        return 0, "", int(ref)
    other, _, number = ref.rpartition("#")
    return 1, other.lower(), int(number) if number.isdigit() else 0


LINK_URL = re.compile(r"github\.com/([^/]+/[^/]+)/(?:issues|pull)/(\d+)")


def link_list(subjects: Sequence[Optional[Dict[str, Any]]], repo_full: str, wanted_type: str,
              exclude: Sequence[str] = ()) -> List[str]:
    """Referenced issues or PRs of type `wanted_type` as '123' (same repository)
    or 'owner/repo#123' (another repository), sorted and without duplicates."""
    refs: Set[str] = set()
    for subject in subjects:
        if not subject or subject.get("__typename") != wanted_type:
            continue
        match = LINK_URL.search(subject.get("url") or "")
        if not match:
            continue
        other, number = match.groups()
        refs.add(number if other.lower() == repo_full.lower() else f"{other}#{number}")
    return sorted(refs - set(exclude), key=_ref_sort_key)


def cross_reference_sources(client: GitHubClient, item: Dict[str, Any],
                            type_name: str) -> List[Optional[Dict[str, Any]]]:
    """Issues and PRs that mention this item ("X mentioned this" in its timeline)."""
    return [event.get("source") for event in all_nodes(client, item, "timelineItems", type_name)]


def actor_login(actor: Optional[Dict[str, Any]]) -> str:
    """Login of an author; deleted accounts (null) become 'ghost'."""
    return (actor.get("login") or "ghost") if actor else "ghost"


def issue_row(client: GitHubClient, issue: Dict[str, Any], repo_full: str) -> Dict[str, Any]:
    """One CSV row for an issue."""
    closing = link_list(all_nodes(client, issue, "closedByPullRequestsReferences", "Issue"),
                        repo_full, "PullRequest")
    mentioned = link_list(cross_reference_sources(client, issue, "Issue"), repo_full,
                          "PullRequest", exclude=closing)
    author = issue.get("author")
    comments = issue.get("comments") or {}
    last_comment = (comments.get("nodes") or [None])[-1] or {}
    return {
        "opened_date": date_only(issue.get("createdAt")),
        "issue_id": issue.get("number"),
        "url": issue.get("url"),
        "state": issue.get("state") or "",
        "state_reason": issue.get("stateReason") or "",
        "title": issue.get("title") or "",
        "opened_by": actor_login(author),
        "author_type": author.get("__typename", "") if author else "",
        "labels": [label["name"] for label in all_nodes(client, issue, "labels", "Issue")],
        "comments_amount": comments.get("totalCount"),
        "reactions_amount": (issue.get("reactions") or {}).get("totalCount"),
        "assignees": [user["login"] for user in all_nodes(client, issue, "assignees", "Issue")],
        "milestone": (issue.get("milestone") or {}).get("title") or "",
        "is_locked": issue.get("locked"),
        "closed_at": iso_utc(issue.get("closedAt")),
        "updated_at": iso_utc(issue.get("updatedAt")),
        "last_comment_date": iso_utc(last_comment.get("createdAt")),
        "closing_links": closing,
        "mentioned_links": mentioned,
    }


def pr_row(client: GitHubClient, pr: Dict[str, Any], repo_full: str) -> Dict[str, Any]:
    """One CSV row for a pull request."""
    closing = link_list(all_nodes(client, pr, "closingIssuesReferences", "PullRequest"),
                        repo_full, "Issue")
    mentioned = link_list(cross_reference_sources(client, pr, "PullRequest"), repo_full,
                          "Issue", exclude=closing)
    author = pr.get("author")
    commits = pr.get("commits") or {}
    last_commit = ((commits.get("nodes") or [None])[-1] or {}).get("commit") or {}
    merged = pr.get("state") == "MERGED" or bool(pr.get("mergedAt"))
    return {
        "opened_date": date_only(pr.get("createdAt")),
        "pr_id": pr.get("number"),
        "url": pr.get("url"),
        "state": pr.get("state") or "",
        "is_draft": pr.get("isDraft"),
        "title": pr.get("title") or "",
        "opened_by": actor_login(author),
        "author_type": author.get("__typename", "") if author else "",
        "user_role": pr.get("authorAssociation") or "",
        "labels": [label["name"] for label in all_nodes(client, pr, "labels", "PullRequest")],
        "comments_amount": (pr.get("comments") or {}).get("totalCount"),
        "reviews_count": (pr.get("reviews") or {}).get("totalCount"),
        "review_decision": pr.get("reviewDecision") or "",
        "base_branch": pr.get("baseRefName") or "",
        "is_cross_repository": pr.get("isCrossRepository"),
        "additions": pr.get("additions"),
        "deletions": pr.get("deletions"),
        "changed_files": pr.get("changedFiles"),
        "commits_count": commits.get("totalCount"),
        "merged_by": actor_login(pr.get("mergedBy")) if merged else "",
        "ci_status": (last_commit.get("statusCheckRollup") or {}).get("state") or "",
        "closed_at": iso_utc(pr.get("closedAt")),
        "merged_at": iso_utc(pr.get("mergedAt")),
        "updated_at": iso_utc(pr.get("updatedAt")),
        "closing_links": closing,
        "mentioned_links": mentioned,
    }


def fetch_issues(client: GitHubClient, ctx: RunContext, repo: str) -> str:
    """Every issue since the repository was created, open and closed (no PRs)."""
    owner, name = split_repo(repo)
    ds = make_dataset(ctx, repo, "issues", label="issues", unit="issues", columns=ISSUES_COLUMNS,
                      tail="", partial_tail="", output_tails=("",))

    def issues_page(cursor: Optional[str], page_size: PageSize, state: Dict[str, Any]) -> Page:
        node = client.graphql(ISSUES_QUERY, {"owner": owner, "name": name, "after": cursor},
                              page_size)["repository"]
        note_rename(ctx, repo, node.get("nameWithOwner"))
        issues = node["issues"]
        rows = [issue_row(client, issue, node["nameWithOwner"])
                for issue in issues.get("nodes") or [] if issue]
        info = issues["pageInfo"]
        return Page(rows, info["endCursor"], info["hasNextPage"], issues["totalCount"])

    return run_dataset(ctx, ds, issues_page, finalize_rename)


def fetch_prs(client: GitHubClient, ctx: RunContext, repo: str) -> str:
    """Every pull request since the repository was created, in all states."""
    owner, name = split_repo(repo)
    ds = make_dataset(ctx, repo, "prs", label="pull requests", unit="PRs", columns=PRS_COLUMNS,
                      tail="", partial_tail="", output_tails=("",))

    def prs_page(cursor: Optional[str], page_size: PageSize, state: Dict[str, Any]) -> Page:
        node = client.graphql(PRS_QUERY, {"owner": owner, "name": name, "after": cursor},
                              page_size)["repository"]
        note_rename(ctx, repo, node.get("nameWithOwner"))
        prs = node["pullRequests"]
        rows = [pr_row(client, pr, node["nameWithOwner"]) for pr in prs.get("nodes") or [] if pr]
        info = prs["pageInfo"]
        return Page(rows, info["endCursor"], info["hasNextPage"], prs["totalCount"])

    return run_dataset(ctx, ds, prs_page, finalize_rename)


# ---------------------------------------------------------------------------
# 5. Repository summary
# ---------------------------------------------------------------------------


def fetch_repo_summary(client: GitHubClient, repo: str) -> Dict[str, Any]:
    """One summary row for a repository (one GraphQL request)."""
    owner, name = split_repo(repo)
    node = client.graphql(SUMMARY_QUERY, {"owner": owner, "name": name})["repository"]
    languages = (node.get("languages") or {}).get("edges") or []
    topics = (node.get("repositoryTopics") or {}).get("nodes") or []
    return {
        "repo": repo,
        "owner_type": (node.get("owner") or {}).get("__typename", ""),
        "created_at": date_only(node.get("createdAt")),
        "pushed_at": iso_utc(node.get("pushedAt")),
        "description": node.get("description") or "",
        "primary_language": (node.get("primaryLanguage") or {}).get("name", ""),
        "languages": [f"{edge['node']['name']}:{edge['size']}" for edge in languages if edge],
        "license": (node.get("licenseInfo") or {}).get("spdxId") or "",
        "topics": [topic["topic"]["name"] for topic in topics if topic],
        "stars_amount": node.get("stargazerCount"),
        "forks_amount": node.get("forkCount"),
        "watchers_amount": (node.get("watchers") or {}).get("totalCount"),
        "open_issues_amount": (node.get("openIssues") or {}).get("totalCount"),
        "open_prs_amount": (node.get("openPullRequests") or {}).get("totalCount"),
        "disk_usage_kb": node.get("diskUsage"),
        "is_fork": node.get("isFork"),
        "parent_repo": (node.get("parent") or {}).get("nameWithOwner", ""),
        "is_archived": node.get("isArchived"),
        "default_branch": (node.get("defaultBranchRef") or {}).get("name", ""),
    }


def summary_repos(path: Path) -> Set[str]:
    """Repositories (lowercase) already listed in an existing summary file."""
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return {row["repo"].lower() for row in csv.DictReader(handle) if row.get("repo")}
    except (OSError, KeyError, csv.Error):
        return set()


def write_summary(client: GitHubClient, ctx: RunContext, repos: List[str]) -> None:
    """repos_summary_[date].csv: one row per repository of the batch.

    A summary already saved for the same date is kept and completed with the
    repositories of the list it does not contain yet (e.g. repositories added
    to the list after a first run), so it always covers the whole batch.
    """
    final = ctx.out_dir / f"repos_summary_{ctx.date}.csv"
    forced = ("summary" in ctx.batch.get("force_kinds", []) and final.exists()
              and final.stat().st_mtime < float(ctx.batch.get("started_ts", 0)) - 2)
    present = summary_repos(final) if final.exists() and not forced else set()
    # Repositories already reported as FAILED (not found...) are left out.
    todo = [repo for repo in repos if repo.lower() not in present and repo not in ctx.fatal_repos]
    if present and not todo:
        CONSOLE.message(f"Summary: {final.name} already saved, skipped")
        return
    partial = final.with_name(final.name + ".partial")
    if present:
        log_event(f"Summary: adding {plural(len(todo), 'repository', 'repositories')} "
                  f"missing from {final.name}")
        shutil.copyfile(final, partial)  # keep the rows saved by earlier runs
    else:
        write_csv(partial, SUMMARY_COLUMNS, [], append=False)
    saved = retry_later = 0
    for repo in todo:
        try:
            row = fetch_repo_summary(client, repo)
        except RepoError as exc:
            log_event(f"FAILED: {repo} - {exc}")
            ctx.failures += 1
            ctx.fatal_repos.add(repo)
            continue
        except Exception as exc:  # FetchError or anything unexpected: retry on the next run
            reason = str(exc) if isinstance(exc, FetchError) else f"unexpected error ({type(exc).__name__}: {exc})"
            log_event(f"FAILED: {repo} summary - {reason}")
            ctx.failures += 1
            retry_later += 1
            continue
        with critical_section():
            write_csv(partial, SUMMARY_COLUMNS, [row])
        saved += 1
        CONSOLE.progress(f"Summary: {saved}/{len(todo)} repositories")
    if retry_later:
        # Never publish a summary with rows missing because of temporary errors.
        ctx.pending += 1
        log_event(f"Summary not saved: {retry_later} repositories failed with temporary errors. "
                  f"Rerun the same command to try again.")
        return
    with critical_section():
        replace_file(partial, final)
    CONSOLE.progress(f"Summary: {plural(saved, 'repository', 'repositories')} saved to "
                     f"{final.name} ({len(summary_repos(final))} in total)", final=True)


# ---------------------------------------------------------------------------
# Batch: repository list, --force, collection date
# ---------------------------------------------------------------------------

REPO_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$")


def read_repo_list(path: Path) -> List[str]:
    """Read 'owner/repo' lines: skip blank lines and '#' comments, accept
    pasted github.com URLs, and drop duplicates keeping the first occurrence."""
    repos: List[str] = []
    seen: Set[str] = set()
    content = path.read_bytes()
    if content.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        text = content.decode("utf-16")  # e.g. written by Windows PowerShell 5 with '>'
    else:
        text = content.decode("utf-8-sig", errors="replace")
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.split("#", 1)[0].strip()  # '#' cannot appear in repository names
        line = re.sub(r"^(?:https?://)?(?:www\.)?github\.com/", "", line).rstrip("/")
        if line.endswith(".git"):
            line = line[:-4]
        if not REPO_PATTERN.match(line):
            CONSOLE.message(f"Skipping line {number} of {path}: '{raw.strip()}' is not owner/repo")
            continue
        if line.lower() not in seen:  # GitHub names are case-insensitive
            seen.add(line.lower())
            repos.append(line)
    return repos


def purge_checkpoints(out_dir: Path, state_dir: Path, repos: List[str], kinds: List[str],
                      summary: bool) -> None:
    """--force: delete the saved progress (.state.json and .partial files) of
    every repository and kind in this run."""
    tail = (r"_\d{4}-\d{2}-\d{2}(?:_complete_history)?"
            r"(?:\.state\.json|\.state\.json\.tmp|\.csv\.partial|\.csv\.tmp)$")
    patterns = [re.compile(re.escape("{}-{}_{}".format(*split_repo(repo), KIND_LABELS[kind])) + tail)
                for repo in repos for kind in kinds]
    if summary:
        patterns.append(re.compile(r"repos_summary_\d{4}-\d{2}-\d{2}\.csv\.(?:partial|tmp)$"))
    removed = 0
    for folder in (state_dir, out_dir):
        for path in folder.iterdir():
            if path.is_file() and any(pattern.match(path.name) for pattern in patterns):
                path.unlink()
                removed += 1
    if removed:
        log_event(f"--force: deleted {removed} files of saved progress; starting over")


def open_batch(state_dir: Path, force: bool, scope: List[str]) -> Tuple[Dict[str, Any], bool]:
    """Load the checkpoint of an unfinished batch (so a resumed batch keeps
    its collection date and skips completed files), or start a new batch
    dated today (UTC). Returns (batch, resumed)."""
    path = state_dir / BATCH_STATE_FILE
    if path.exists() and not force:
        try:
            batch = json.loads(path.read_text(encoding="utf-8"))
            dt.date.fromisoformat(batch["collection_date"])
            return batch, True
        except (OSError, ValueError, KeyError, TypeError):
            pass  # unreadable: start a new batch
    now = utc_now()
    batch = {
        "collection_date": now.strftime("%Y-%m-%d"),
        "started_at": now.strftime(ISO_FORMAT),
        "started_ts": now.timestamp(),
        # With --force, final files of these kinds written before started_ts
        # are refetched, even when an interrupted forced run is resumed.
        "force_kinds": scope if force else [],
    }
    write_json_atomic(path, batch)
    return batch, False


def close_batch(ctx: RunContext) -> None:
    """Delete the batch checkpoint once nothing is left to resume."""
    if ctx.pending:
        return
    (ctx.state_dir / BATCH_STATE_FILE).unlink(missing_ok=True)
    try:
        ctx.state_dir.rmdir()  # only succeeds when the folder is empty
    except OSError:
        pass


def resume_command(argv: Optional[Sequence[str]]) -> str:
    """The command to type to resume (the same one, without --force)."""
    args = [arg for arg in (sys.argv[1:] if argv is None else argv) if arg != "--force"]
    script = Path(sys.argv[0]).name if sys.argv[0].endswith(".py") else "github_repo_metadata.py"
    return "python " + " ".join(shlex.quote(part) for part in [script] + args)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

FETCHERS: Dict[str, Callable[[GitHubClient, RunContext, str], str]] = {
    "stars": fetch_stars,
    "commits": fetch_commits,
    "issues": fetch_issues,
    "prs": fetch_prs,
}


def process_repo(client: GitHubClient, ctx: RunContext, repo: str, kinds: List[str]) -> None:
    """Fetch every requested kind of one repository, then log one summary line."""
    started, points = time.monotonic(), client.points_used
    outcomes: List[str] = []
    for kind in kinds:
        try:
            outcomes.append(f"{kind}: {FETCHERS[kind](client, ctx, repo)}")
        except AuthError as exc:  # token rejected mid-run: keep all progress for a rerun
            log_event(f"FAILED: {repo} - {exc}")
            ctx.failures += 1
            ctx.pending += 1
            outcomes.append(f"{kind}: FAILED")
            break
        except RepoError as exc:  # the whole repository is unusable: skip its other kinds
            log_event(f"FAILED: {repo} - {exc}")
            ctx.failures += 1
            ctx.fatal_repos.add(repo)
            outcomes.append(f"{kind}: FAILED")
            break
        except FetchError as exc:  # only this kind failed; its checkpoint is kept
            log_event(f"FAILED: {repo} {kind} - {exc}")
            ctx.failures += 1
            ctx.pending += 1
            outcomes.append(f"{kind}: FAILED")
        except Exception as exc:  # anything unexpected: report it, never abort the batch
            traceback.print_exc()
            log_event(f"FAILED: {repo} {kind} - unexpected error ({type(exc).__name__}: {exc})")
            ctx.failures += 1
            ctx.pending += 1
            outcomes.append(f"{kind}: FAILED")
    line = (f"{repo} | collection date {ctx.date} | " + " | ".join(outcomes)
            + f" | {fmt_duration(time.monotonic() - started)}, "
              f"{client.points_used - points} GraphQL points")
    CONSOLE.message("  Done: " + line)
    log_line(line)


def parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download metadata (stars, commits, issues, pull requests) for a list "
                    "of GitHub repositories and save it as CSV files.")
    parser.add_argument("--repos", default="repos.txt",
                        help="text file with one owner/repo per line (default: repos.txt)")
    parser.add_argument("--out-dir", default="./output",
                        help="folder for the CSV files (default: ./output)")
    parser.add_argument("--only", choices=["stars", "commits", "issues", "prs", "summary"],
                        help="fetch only this kind of data")
    parser.add_argument("--force", action="store_true",
                        help="ignore existing files and saved progress, and start over")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("Error: the GITHUB_TOKEN environment variable is not set.\n"
              "Create a token at https://github.com/settings/tokens, then set it for this "
              "terminal session:\n"
              "  macOS / Linux:  export GITHUB_TOKEN=ghp_your_token\n"
              "  PowerShell:     $env:GITHUB_TOKEN=\"ghp_your_token\"\n"
              "See the 'Token' section of README.md.", file=sys.stderr)
        return 2
    repos_path = Path(args.repos)
    if not repos_path.is_file():
        print(f"Error: the repository list '{repos_path}' does not exist. Create it with one "
              f"owner/repo per line (see README.md).", file=sys.stderr)
        return 2
    repos = read_repo_list(repos_path)
    if not repos:
        print(f"Error: '{repos_path}' does not list any repository (one owner/repo per line).",
              file=sys.stderr)
        return 2

    kinds = [] if args.only == "summary" else ([args.only] if args.only else list(KINDS))
    do_summary = args.only in (None, "summary")
    out_dir = Path(args.out_dir)
    state_dir = out_dir / STATE_DIR_NAME
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"Error: cannot create the output folder '{out_dir}': {exc}", file=sys.stderr)
        return 2

    client = GitHubClient(token)
    try:
        login, points_left = client.check_token()
    except AuthError:
        print("Error: GitHub rejected GITHUB_TOKEN (401 Bad credentials). Check that the whole "
              "token was copied and that it has not expired or been revoked.", file=sys.stderr)
        return 2
    except (RepoError, FetchError) as exc:
        print(f"Error: could not reach GitHub: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130

    if args.force:
        purge_checkpoints(out_dir, state_dir, repos, kinds, do_summary)
    batch, resumed = open_batch(state_dir, args.force, kinds + (["summary"] if do_summary else []))
    ctx = RunContext(out_dir=out_dir, state_dir=state_dir, date=batch["collection_date"], batch=batch)

    print(f"GitHub repository metadata: {plural(len(repos), 'repository', 'repositories')} "
          f"from {repos_path}")
    print(f"  output folder: {out_dir}    collection date: {ctx.date} (UTC)")
    print(f"  authenticated as {login}; GraphQL points left this hour: {points_left}")
    log_line(f"=== Run started: {len(repos)} repositories from {repos_path}, collection date "
             f"{ctx.date}, only={args.only or 'all'}, force={args.force}")
    if resumed:
        log_event(f"Resuming an unfinished batch started on {batch.get('started_at', ctx.date)}: "
                  f"completed files are skipped and all files keep the collection date {ctx.date} "
                  f"(use --force to start a new collection instead).")

    started = time.monotonic()
    try:
        for index, repo in enumerate(repos, start=1):
            CONSOLE.message(f"[{index}/{len(repos)}] {repo}")
            process_repo(client, ctx, repo, kinds)
        if do_summary:
            write_summary(client, ctx, repos)
    except KeyboardInterrupt:
        CONSOLE.newline()
        print("\nInterrupted. Everything downloaded so far is saved.\n"
              "To resume, run the same command again:\n    " + resume_command(argv))
        log_line("INTERRUPTED (Ctrl+C); rerun the same command to resume")
        return 130

    close_batch(ctx)
    status = f"{ctx.failures} FAILED" if ctx.failures else "no failures"
    print(f"\nFinished in {fmt_duration(time.monotonic() - started)}: "
          f"{plural(len(repos), 'repository', 'repositories')}, {status}, "
          f"{client.points_used:,} GraphQL points used. Files are in {out_dir}")
    if ctx.pending:
        print("Some fetches did not complete; rerun the same command to resume them.")
    log_line(f"=== Run finished: {status}, {client.points_used} GraphQL points used")
    return 1 if ctx.failures else 0


if __name__ == "__main__":
    sys.exit(main())
