# CourseLink MCP

### Your courses, one conversation.

Connect University of Guelph CourseLink to your AI assistant. Find assignment instructions, check deadlines, and catch updates without clicking through every course.

## Why you'll want it

- **Skip the scavenger hunt.** Search across course materials and read PDFs, documents, and starter files right from your assistant.
- **Catch what's changed.** See new assignments, announcements, and updated files in one place.
- **Keep your materials handy.** Automatically download files and preserve older versions when they change.
- **Pick up where you left off.** Search and read saved materials even when you're signed out.

## Just ask

> “What assignments are due next?”
>
> “Find the instructions and rubric for this week's lab.”
>
> “What's changed since I last checked?”
>
> “Search my course documents for this topic.”

Your assistant can look up the actual material, so you spend less time finding it and more time working on it.

## Get started

You'll need macOS or Linux, Python 3.11–3.14, and [uv](https://docs.astral.sh/uv/).

Clone this repo, then run:

```sh
cd courselink-mcp
uv sync --locked
uv run playwright install chromium
uv run courselink init
uv run courselink login
```

Sign in through the browser, add your courses to the private config printed by `init`, then run `uv run courselink serve`. Connect your assistant using [mcp-client.json](mcp-client.json), replacing the example path with your clone's location.

**[Full setup guide →](docs/setup.md)**

Run it on your computer, or on your own server for continuous monitoring. Your account, session, and downloaded materials stay on the host you choose; your connected assistant can access the course data you ask it to read. Monitoring needs a running service and a valid login.

[MIT License](LICENSE)
