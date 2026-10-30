# CourseLink MCP

<p align="center"><img src="assets/alfred-courselink.png" alt="Alfred mid-air hammer-smashing the CourseLink logo, with flying files and debris: downloaded." width="640"></p>

got tired of opening CourseLink, clicking through every course, and downloading assignments one by one. so i made this. connect your own University of Guelph CourseLink account to your AI assistant and just ask.

## what you can do

- **check in from chat.** after connecting your monitor, select CourseLink in ChatGPT (or mention `@CourseLink` when available) and ask “anything new?” or “what’s due next?” Claude works through connectors too. you don’t need a coding agent for everyday use.
- **get the assignment details.** ask “what do i need for this assignment?” to find instructions and search downloaded documents.
- **have files ready to work with.** the monitor downloads course files and keeps older versions when they change. if it runs remotely, optional sync brings them to your computer. open the course folder in Codex, Claude Code, or another local assistant to get help with the material.
- **check your grades.** `get_grades(course_id=None)` reads released grades for one or all monitored courses, including points, weights, categories, and released calculated/adjusted final grades. Results include per-course freshness and coverage errors.

To check grades through an already running monitor, use `uv run courselink grades --course example-course --json` (omit `--course` for all monitored courses). Add `--refresh` for a live fetch through its existing session. This command never starts the monitor or opens sign-in. Grades use an on-demand cache; regular background material scans do not refresh them.

**Quiz attempts:** `list_quizzes(course_id, quiz_id=None)` browses learner summaries; `quiz_status(course_id, query)` answers whether a quiz was attempted, how many attempts are completed, and whether it is still open. Both are read-only and never start or submit a quiz. See [quiz tracking, scores and coverage](docs/quizzes.md).

**Submission history and uploads:** `list_submissions` and `submission_status` answer what was submitted and when. `submit_to_dropbox` previews by default; an actual upload requires explicit confirmation with its single-use preview token, plus separate late approval when past due. See the [tool schemas, safety rules and offline limitations](docs/submissions.md).


routine update checks read the cache immediately. background metadata scans target every 5 minutes while running; unchanged-looking files get byte checks hourly. results include when the data was last checked.

## where it runs

- **on your computer by default.** no separate server needed. monitoring runs while your computer is awake and the process is running. Claude Desktop can connect locally, and saved files stay available offline.
- **on a server if you want it always running.** handy when your laptop is off or you’re on the go: open a supported chat client, select CourseLink, and see what’s up. ChatGPT and Claude web need a supported remote connection or authenticated tunnel to reach the monitor—even when it runs on your computer. remote access takes extra setup.

## getting started

clone this repo and follow the [setup guide](docs/setup.md), or ask a coding assistant: **“set this up using [AGENTS.md](AGENTS.md).”** it handles installation and configuration; you sign in through the browser, complete MFA, and choose your courses. if CourseLink expires your session, sign in again.

[Claude chat setup](docs/setup.md#use-courselink-in-claude-chat) · [ChatGPT connection guide](https://developers.openai.com/plugins/deploy/connect-chatgpt) · [server reliability](docs/reliability.md)

made by [@CourseLink MCP contributors](https://example.invalid/courselink).

## cloud-generated deliverables

An authenticated coding assistant can read owner-provided instructions and the
initial discovery baseline with `automation_context`, then save requested ZIP
deliverables with `save_completed_work`. This private mailbox never submits work
to CourseLink. Archives are immutable, limited to 6 MiB, and include source item
IDs plus an honest validation report in their metadata. The monitor does not
execute or extract them.

On an already configured computer bridge, `courselink sync-results` downloads
these ZIPs into the mapped course’s `CourseLink Downloads/labs/labN/submission/` or
`CourseLink Downloads/assignments/aN/submission/` directory. It verifies hashes and sizes, preserves
local edits, and returns a nonzero status on errors. Schedule that command with
the computer's service manager for automatic retrieval while it is online.
The cloud coding schedule is configured separately in the assistant; this MCP
does not itself run coding agents. Owner instruction snapshots belong in the
private state directory's `automation-context.json`, never in the clone.
