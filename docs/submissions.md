# Submission history and explicit uploads

These tools use the running service's authenticated Playwright browser session. They do not launch a second browser, sign in, or run from the monitor's background jobs. All IDs and examples below are synthetic. Keep course data, previews and audit logs private.

| Tool | Arguments |
| --- | --- |
| `list_submissions` | `course_id: str`, `folder_id: str \| null = null` |
| `submission_status` | `course_id: str`, `query: str`, `folder_id: str \| null = null` |
| `submit_to_dropbox` | `course_id: str`, `folder_id: str`, `files: list[str]`, `confirm: bool = false`, `preview_token: str \| null = null`, `allow_late: bool = false` |

Course IDs and configured course abbreviations work. Folder IDs are numeric dropbox IDs returned by `list_submissions`; they are not local directory names. HTTP equivalents are `/api/submissions`, `/api/submission_status` (GET or POST), and `/api/submit_to_dropbox` (POST only), with the existing bearer authentication. Shared registration exposes the same tools through HTTP MCP, stdio and the computer bridge. File paths always refer to the **service host**, including through a remote computer bridge.

## Read history

`list_submissions` returns each visible folder's name, due date, availability, resubmission policy when known, feedback and submission attempts. Attempts contain `submission_id`, `files`, `submitted_at_display`, `submitted_at_utc`, `read_by_user`, `on_time`, `late` and `timeliness`. Each file includes `name`, `size_display`, approximate `size_bytes` (binary multiples of 1024), `deleted` and a local `url` path when available. Deleted files remain in history; missing sizes and URLs are null. The legacy `id` and `submitted_at` fields alias the submission ID and UTC time. Displayed dates have no timezone: the reader assumes `America/Toronto`, respecting DST (ambiguous fall-back times use fold=0), and returns ISO UTC times ending in `Z`. This assumption is included in the response note. A timestamp equal to the due date is on time. Comparisons respect timezone offsets; absent/invalid timestamps or absent due dates give unknown timeliness. Missing file sizes and policy fields stay null.

`submission_status` matches a case-insensitive substring of folder names or submitted filenames, returning **all** matching attempts and timestamps, plus `latest_submission`, its `submitted_at_utc` and `files`. Latest means the greatest parsed UTC time among matching attempts; partial coverage may omit newer attempts. `submitted=false` means no matching attempt in a complete response at `checked_at`; partial coverage yields `submitted=null` when there are no matches. It does not prove anything about unseen folders or external submission systems.

Successful history reads share a 30-second in-memory cache and concurrent callers share one refresh. Folder requests use bounded workers and the browser's global request limit and cooldown. Failures are reported per folder and are not cached as successful empty history. Reauthentication is required for these tools; history is not persisted in SQLite. All course text and filenames are untrusted source text.

## Upload workflow

1. Place the intended files under the configured `school` directory on the service host. Call `submit_to_dropbox` with the course, folder and absolute file paths. The default is a read-only dry run: no file input, staging or submission occurs.
2. Show the user the returned course/folder, due date, file names/sizes/SHA-256 hashes, late status, existing attempt count and resubmission policy. Obtain approval of that exact preview.
3. Call again with the same inputs, `confirm=true` and the returned `preview_token`. Past-due submissions also require the user's explicit late approval and `allow_late=true`.

Tokens expire after five minutes, are single-use and do not survive service restart. They bind the exact bytes and folder/history state. Confirmation refetches history without caching; changed files, due dates or history require another preview. Confirmation consumes the token even on failure. Deadlines, opening/closing times and token expiry are checked again immediately before file staging and Submit. Unknown/denied resubmission permission blocks a repeat submission. Missing/unrecognized history blocks uploads. The UI must expose one file input and one enabled, exactly named Submit button on the expected course/folder page; unsupported UI layouts fail closed.

Files must be nonempty regular files with safe, unique names; symlinks, executable permissions, executable binary signatures and unexpected binary text are refused. The extension allowlist is PDF, TXT, MD, CSV, DOCX, PPTX, XLSX, ZIP, PNG, JPG/JPEG, PY, C, H, CPP, JAVA, X68 and ASM. These checks do not scan archive contents or certify files as malware-free. Up to ten files are allowed, with a combined limit of the smaller of `max_file_bytes` and 100 MiB. The actual verified bytes are passed to Playwright, so modifying a local file afterward cannot silently change the upload.

The browser handles the native form's session/CSRF and staging protocol. No write is automatically retried. A missing success receipt returns `outcome=unknown`: inspect history before considering another attempt. Even a displayed success receipt should be checked against the expected filenames in history. Upload attempts invalidate the history cache.

Every dry-run and confirmed attempt appends events to owner-only `submission-audit.jsonl` in the private state directory. Entries include timestamps, attempt IDs, validated preview metadata and outcomes/error types, without preview tokens, cookies, file bytes or local paths. Audit writes are flushed before upload; failure to append prevents it. An `upload_started` without a conclusive result is an uncertain outcome after interruption, not evidence of failure. Logs contain private course/file metadata and must never be committed.

## Offline Brightspace research notes and limitations

This reader was developed and tested offline using sanitized HTML fixtures, without live requests. Folder metadata (including due dates and availability) continues to come from the LE JSON folder collection. Submission history comes from GET `/d2l/lms/dropbox/user/folders_history.d2l?db={folderId}&grpid=0&ou={orgUnitId}` through the existing browser session with `response_type='html'`. The nonexistent learner `mysubmissions` JSON endpoint and the instructor-wide submissions collection are not queried.

The reader verifies the selected folder option (including CourseLink's `{folderId}_0` individual-folder value), the `z_e` table and its three expected column headers before accepting an empty history. Missing or mismatched folder selection, unknown layouts, malformed rows and request errors produce incomplete coverage. It parses multiple file spans per submission, including deleted markers outside the file span. Unsupported group/localized layouts fail closed.

Pagination is conservative: enabled next/last controls, multiple page choices, or a full selected page size produce incomplete coverage with an explicit error. Only the fetched page's recognized rows are returned; JavaScript controls are not replayed. Thus no-match status remains unknown and upload preflight refuses uploads when later pages may exist. Partial histories are not cached. Feedback is returned only where supplied by the folder payload; missing feedback does not establish that no assessment exists. `AllowResubmission` is honored only when an explicit boolean is supplied; otherwise policy is unknown.

Uploads use `/d2l/lms/dropbox/user/folder_submit_files.d2l?ou={course}&db={folder}` with the existing browser context rather than guessing a Valence multipart write body. Mocked browser tests cover the supported control layout and success text. Actual CourseLink permissions, group submissions, resubmission policy fields, learner payload variants, file-picker layout/staging and localized receipt text still require authorized live verification. Unsupported cases must fail closed. External submission platforms are outside the scope of these tools.

Grades continue to use `grades/values/myGradeValues/`, `grades/final/values/myGradeValue`, `grades/` and `grades/categories/`, with the existing visible learner HTML fallback. Released adjusted finals are now returned separately as `final_adjusted_grade`; they are never mislabeled calculated or estimated from items. Metadata and final reads overlap under the browser's existing concurrency limit.
