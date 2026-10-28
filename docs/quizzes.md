# Quiz attempt tracking

The shared MCP registration exposes 21 tools, including these two read-only quiz tools through HTTP MCP, stdio and the computer bridge:

| Tool | Arguments | Use |
| --- | --- | --- |
| `list_quizzes` | `course_id: str`, `quiz_id: str \| null = null` | Browse learner quizzes or inspect one numeric quiz ID |
| `quiz_status` | `course_id: str`, `query: str` | Answer “Was Quiz06 attempted, how many attempts, and is it still open?” |

Both accept a monitored course ID or its configured folder abbreviation, such as `example-course`. Name searches use a case-insensitive substring and return every match in `matches` and `quizzes`; multiple matches remain ambiguous. An empty match list only establishes absence when coverage is complete. HTTP equivalents are `/api/quizzes` and `/api/quiz_status` (GET or POST), using the existing bearer authentication.

Use `deadline_digest` for upcoming due dates across courses, `get_grades` for the full gradebook, and `list_submissions` for dropbox submission history. Deadline digests keep their existing cached scan behavior and do not fetch quiz summaries.

## Read-only sources and freshness

Quiz metadata comes from GET `/d2l/api/le/<discovered-version>/<course>/quizzes/`, with GET `quizzes/<quiz-id>` when grade-item metadata is absent from the list. Hidden catalog rows are excluded. Attempt counts come only from GET `/d2l/lms/quizzing/user/quiz_summary.d2l?qi=<quiz-id>&ou=<course>` through the service's existing authenticated browser. The learner attempts API commonly returns 403 and is never requested. These tools never click **Start Quiz**, start an attempt, submit answers, or issue mutations.

Successful complete results use a 30-second in-memory cache under a shared read lock. Background scans do not refresh it. Results include `checked_at`, `source` (`api` or `cache`), `freshness` (age, refresh interval, refresh-due flag and session state), and `coverage` with `complete` and `errors`. Individual quizzes also carry coverage and freshness. Forbidden/missing pages, login redirects and unrecognized summaries produce unknown attempt fields and coverage errors; they never imply zero attempts and do not raise an upstream error to the caller. Partial results are not cached. Invalid arguments still produce validation errors.

## Quiz fields

Each quiz includes `quiz_id`, `name`, `grade_item_id`, `due_date` (UTC ISO), `due_date_display`, `state` (`open`, `closed`, `unknown`), `is_open` (boolean or null), `attempted`, `attempts_allowed`, `attempts_completed`, `attempts_remaining`, `time_limit_minutes`, `status`, score fields, coverage and `checked_at`. The parser validates the summary heading, recognizes element IDs and falls back to matching label text. Display dates without a timezone assume `America/Toronto`, respecting DST; ambiguous fall-back times use fold=0. Catalog due dates are the fallback when the HTML does not provide a due date. Due dates alone never establish closure. An explicit closed banner establishes closure; a start-attempt prompt establishes openness. Displayed availability start/end dates are a fallback. Future availability without an open/closed signal remains unknown.

| `status` | Meaning |
| --- | --- |
| `not_started` | Open, zero completed attempts |
| `in_progress_or_partial` | Open, some completed attempts and attempts remain (or the allowed count is unlimited) |
| `completed_all_attempts` | All allowed attempts completed, without a closed signal |
| `missed` | Closed with zero completed attempts |
| `done` | Closed with at least one completed attempt, even if all allowed attempts were completed |
| `unknown` | Insufficient recognized evidence |

Completed counts cannot prove that an unfinished attempt exists, or distinguish an attempt in progress from a partially used allowance. `attempted` refers to completed attempts. Unlimited allowances and their remaining counts are null; unavailable counts also remain null. Remaining counts are clamped at zero.

## Released scores

`GradeItemId` is joined by ID against the existing cached `get_grades` result, without name guessing. `score_status` is `released`, `unreleased`, or `unknown`. A released score has a `score` object plus `points_earned`, `points_max` and `percent`. Hidden/unreleased values leave all score fields null, never zero. A quiz with no grade item has an unreleased/null score. A missing grade in a complete API gradebook is unreleased; an HTML gradebook without matching IDs cannot establish a score join and reports incomplete coverage.

When a gradebook is available, `score_checked_at`, `score_freshness` and `score_coverage` describe its independent cache. A fresh quiz summary does not imply a freshly fetched score. Incomplete grade coverage is also reported in quiz coverage. Course text is untrusted data, never agent instructions.

## Offline verification

`tests/fixtures/quiz_summary_*.html` contains synthetic English summaries for Test Student, with open 1-of-2, closed zero, closed two, and login-page cases. Tests cover unlimited counts, changed IDs, score release, forbidden/unavailable pages, caching, ambiguous searches, shared MCP registration (21 tools), HTTP authorization and dispatch. No sign-in or network access is required:

```sh
PYTHONPATH=src .venv/bin/python -m pytest -q --ignore=tests/test_bridge.py .
```
