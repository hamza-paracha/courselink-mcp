# CourseLink MCP

<p align="center"><img src="assets/alfred-courselink.png" alt="Alfred mid-air hammer-smashing the CourseLink logo, with flying files and debris: downloaded." width="640"></p>

got tired of opening CourseLink, clicking through every course, and downloading assignments one by one. so i made this. connect your own University of Guelph CourseLink account to your AI assistant and just ask.

## new in v0.2.0

Grades, quiz attempts, submission history, deadlines, and saved deliverables are available through the same monitor. Ask in plain language—there’s no need to remember tool names.

| Ask your assistant | What CourseLink can tell you |
| --- | --- |
| “What grade did I get on my OOP midterm?” | Released marks, points, percentages, weights, categories, and released final grades |
| “Did I finish Quiz 6? Is it still open?” | Completed attempt counts, availability, remaining attempts when available, and released scores |
| “Did I submit my assignment? Was it late?” | Submission attempts, filenames, timestamps, lateness, comments, and feedback |
| “What’s due this week?” | Upcoming deadlines from cached course material |
| “Anything new?” | Cached announcements, newly discovered material, and changed files, with freshness information |
| “Find the instructions for my next lab.” | Assignment and lab pages, downloaded documents, and searchable source text |

- **Read your actual grades.** `get_grades` reads the learner gradebook for one or all monitored courses. Unknown and unreleased scores stay unknown; they are never treated as zero. Results report when grades were checked and any coverage gaps.
- **Track quizzes without opening an attempt.** `list_quizzes` and `quiz_status` read learner summaries. They never start or submit quizzes. See [quiz tracking and score coverage](docs/quizzes.md).
- **Check submission history and prepare uploads.** `list_submissions` and `submission_status` answer what was submitted and when. `submit_to_dropbox` previews files first; uploading requires explicit approval of that exact preview and its single-use token. Late uploads require separate approval. See [submission tools and limitations](docs/submissions.md).
- **Keep requested deliverables ready for download.** A private completed-work mailbox lets an authenticated assistant save generated ZIPs with source references and validation reports. Computer sync verifies their hashes and preserves local edits. Saving to the mailbox never submits to CourseLink.
- **Use the same tools through the computer bridge.** v0.2.0 includes complete bridge forwarding for the expanded tool set.

## files ready when you need them

The monitor downloads accessible course files and retains older versions when their bytes change. Search downloaded text, read instructions or individual ZIP members, and retrieve original files for diagrams or scanned pages. Optional computer sync brings remote downloads to your local course folders.

Routine update questions read the cache immediately. Background metadata scans target every five minutes while the monitor is running; unchanged-looking files get byte checks hourly. Results include freshness and coverage so an expired session or incomplete scan is visible. Grades use a separate on-demand cache; background material scans do not refresh them.

To check grades from the CLI against an already running monitor:

```sh
uv run courselink grades --course example-course --json
```

Omit `--course` for all monitored courses. Add `--refresh` to bypass the grades cache through the existing session. This command never starts the monitor or opens sign-in.

## getting started

Clone this repo and follow the [setup guide](docs/setup.md), or ask a coding assistant: **“set this up using [AGENTS.md](AGENTS.md).”** It handles installation and configuration; you sign in through the browser, complete MFA, and choose your courses. If CourseLink expires your session, sign in again.

- **On your computer by default.** No separate server is needed. Monitoring runs while your computer is awake and the process is running. Local MCP clients can connect directly, and saved files stay available offline.
- **On a server when you need it running independently.** Useful when your laptop is off. ChatGPT and Claude web need a supported remote connection or authenticated tunnel to reach the monitor. Remote access takes extra setup.

[Setup and tool reference](docs/setup.md) · [Claude chat setup](docs/setup.md#use-courselink-in-claude-chat) · [ChatGPT connection guide](https://developers.openai.com/plugins/deploy/connect-chatgpt) · [Server reliability](docs/reliability.md)

### already connected? refresh your tools

After updating the monitor, reload your MCP client’s connection so it discovers the latest tools and instructions. For a custom CourseLink connection in ChatGPT, open its plugin settings, select **Refresh tools**, then start a new conversation. Updating the server alone can leave ChatGPT using an older tool list.

Then ask normally: **“What grade did I get on my midterm?”** The server includes tool-routing instructions for grades, quizzes, submissions, deadlines, and update questions. If the assistant cannot access grades, check that the refreshed connection exposes `get_grades` before relying on an announcement about grade release.

## cloud-generated deliverables

An authenticated coding assistant can read owner-provided instructions and the initial discovery baseline with `automation_context`, then save requested ZIP deliverables with `save_completed_work`. Archives are immutable, limited to 6 MiB, and include source item IDs plus a validation report in their metadata. The monitor does not execute or extract them.

On an already configured computer bridge, `courselink sync-results` downloads these ZIPs into the mapped course’s `CourseLink Downloads/labs/labN/submission/` or `CourseLink Downloads/assignments/aN/submission/` directory. It verifies hashes and sizes, preserves local edits, and returns a nonzero status on errors. Schedule that command with the computer’s service manager for automatic retrieval while it is online.

The cloud coding schedule is configured separately in the assistant; this MCP does not itself run coding agents. Owner instruction snapshots belong in the private state directory’s `automation-context.json`, never in the clone.

made by [@CourseLink MCP contributors](https://example.invalid/courselink).
