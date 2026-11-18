# CourseLink MCP

<p align="center"><img src="assets/alfred-courselink.png" alt="Alfred smashes the CourseLink logo with a mallet and says “downloaded.”" width="560"></p>

**Find out what’s due, what changed, and where the instructions are.** CourseLink MCP connects your University of Guelph CourseLink account to an MCP-compatible assistant. It monitors the courses you choose, downloads files in the background, and keeps older versions when files change.

[Quick start](#quick-start) · [Setup and tool reference](docs/setup.md) · [Remote hosting and Mac sync](#run-it-on-a-server)

## What you can ask

| Ask your assistant | What you get |
| --- | --- |
| “What’s due this week?” | Upcoming assignments, quizzes, and other deadlines across your monitored courses |
| “Anything new since my last check?” | New announcements, updated materials, and file changes, with the last check time |
| “Find the Lab 4 instructions and starter ZIP.” | Links, searchable document text, and original files |
| “Did I submit homework.zip? Was it late?” | Matching attempts, filenames, timestamps, and lateness |
| “Is Quiz 6 still open? What did I score?” | Quiz availability, completed attempts, and released grades |

CourseLink access is read-only. Routine updates come from a local cache, so the assistant can tell you when it last checked and flag incomplete results. Cached materials remain available if your login expires.

## Quick start

You’ll need macOS or Linux, Python 3.11–3.14, and [uv](https://docs.astral.sh/uv/). Clone this repository, then run these commands from its root.

```sh
uv sync --locked
uv run playwright install chromium
uv run courselink init
uv run courselink login
uv run courselink start
```

Sign in and complete MFA in the dedicated browser window when `login` opens. After the monitor finishes discovering your courses, run:

```sh
uv run courselink courses
uv run courselink configure --course 123456=example-course
uv run courselink check
uv run courselink mcp-config
```

Replace `123456` with a course ID from the list. Add the generated MCP entry to your client and reconnect. For guided setup, ask your coding assistant to follow [AGENTS.md](AGENTS.md).

## Run it on a server

Host the monitor on Linux to keep it running while your laptop is off. Use an authenticated connection for remote MCP access. The Mac bridge can bring files and course catalogs into your local course folders. It verifies downloads and preserves local edits. [Server and Mac setup](docs/setup.md#optional-linux-service)

The private mailbox can hold requested ZIP deliverables from a connected coding assistant. Run `courselink sync-results` from a configured Mac bridge to retrieve them. [Mailbox setup](docs/setup.md#completed-work-mailbox)

## Reference

[Quiz tracking](docs/quizzes.md) · [Submission history](docs/submissions.md) · [Watchdog and backups](docs/reliability.md) · [MIT license](LICENSE)
