# CourseLink MCP

<p align="center"><img src="assets/alfred-courselink.png" alt="Alfred mid-air hammer-smashing the CourseLink logo, with flying files and debris: downloaded." width="640"></p>

got tired of opening CourseLink, clicking through every course, and downloading assignments one by one. so i made this. connect your own University of Guelph CourseLink account to your AI assistant and just ask.

## what you can do

- **check in from chat.** after connecting your monitor, select CourseLink in ChatGPT (or mention `@CourseLink` when available) and ask “anything new?” or “what’s due next?” Claude works through connectors too. you don’t need a coding agent for everyday use.
- **get the assignment details.** ask “what do i need for this assignment?” to find instructions and search downloaded documents.
- **have files ready to work with.** the monitor downloads course files and keeps older versions when they change. if it runs remotely, optional sync brings them to your computer. open the course folder in Codex, Claude Code, Hermes, or another local assistant to get help with the material.

routine update checks read the cache immediately. background metadata scans target every 5 minutes while running; unchanged-looking files get byte checks hourly. results include when the data was last checked.

## where it runs

- **on your computer by default.** no separate server needed. monitoring runs while your computer is awake and the process is running. Claude Desktop can connect locally, and saved files stay available offline.
- **on a server if you want it always running.** handy when your laptop is off or you’re on the go: open a supported chat client, select CourseLink, and see what’s up. ChatGPT and Claude web need a supported remote connection or authenticated tunnel to reach the monitor—even when it runs on your computer. remote access takes extra setup.

## getting started

clone this repo and follow the [setup guide](docs/setup.md), or ask a coding assistant: **“set this up using [AGENTS.md](AGENTS.md).”** it handles installation and configuration; you sign in through the browser, complete MFA, and choose your courses. if CourseLink expires your session, sign in again.

[Claude chat setup](docs/setup.md#use-courselink-in-claude-chat) · [ChatGPT connection guide](https://developers.openai.com/plugins/deploy/connect-chatgpt) · [server reliability](docs/reliability.md)

made by [@hamza-paracha](https://github.com/hamza-paracha).
