# Submission history

Use `list_submissions(course_id, folder_id=None, query=None)` to review learner submission history. Course IDs and configured course abbreviations work. An optional numeric folder ID scopes the history to one Dropbox folder.

Ask your assistant “Did I submit homework.zip, and was it late?” A request such as `list_submissions(course_id="example-course", query="homework.zip")` searches folder names and submitted filenames by case-insensitive substring. It returns all matching attempts, their files and lateness, and the latest matching submission by parsed UTC timestamp. An empty result has `submitted=false` when coverage is complete and `submitted=null` when coverage is partial. Without a query, the response lists folders, their attempts, and available comments and feedback.

The equivalent authenticated HTTP operation is `/api/submissions` (GET or POST). The older `/api/submission_status` route remains a read-only compatibility alias.

## Offline Brightspace research notes and limitations

This reader was developed and tested offline using sanitized HTML fixtures, without live requests. Folder metadata (including due dates and availability) continues to come from the LE JSON folder collection. Submission history comes from GET `/d2l/lms/dropbox/user/folders_history.d2l?db={folderId}&grpid=0&ou={orgUnitId}` through the existing browser session with `response_type='html'`. The nonexistent learner `mysubmissions` JSON endpoint and the instructor-wide submissions collection are not queried.

The reader verifies the selected folder option (including CourseLink's `{folderId}_0` individual-folder value), the `z_e` table and its three expected column headers before accepting an empty history. Missing or mismatched folder selection, unknown layouts, malformed rows and request errors produce incomplete coverage. It parses multiple file spans per submission, including deleted markers outside the file span. Unsupported group/localized layouts fail closed.

Pagination is conservative: enabled next/last controls, multiple page choices, or a full selected page size produce incomplete coverage with an explicit error. Only the fetched page's recognized rows are returned; JavaScript controls are not replayed. Thus no-match status remains unknown when later pages may exist. Partial histories are not cached. Feedback is returned only where supplied by the folder payload; missing feedback does not establish that no assessment exists. `AllowResubmission` is honored only when an explicit boolean is supplied; otherwise policy is unknown.
