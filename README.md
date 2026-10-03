# CourseLink MCP

<p align="center"><img src="assets/alfred-courselink.png" alt="Alfred mid-air hammer-smashing the CourseLink logo, with flying files and debris: downloaded." width="640"></p>

got tired of opening CourseLink, clicking through every course, and downloading assignments one by one. so i made this. connect it to ChatGPT, Claude, or another assistant that supports MCP and just ask.

- **use it right in chat.** once you connect your own monitor as a private ChatGPT plugin, select CourseLink in the chat (or mention `@CourseLink` when available) and ask about your courses. you can use it entirely through chat after setup, without opening a coding agent or running commands for each question. [OpenAI’s connection guide](https://developers.openai.com/plugins/deploy/connect-chatgpt) covers connecting and selecting the plugin.
- **Claude chats work too.** Claude Desktop can connect to the local monitor through its MCP configuration, then you enable CourseLink from **+ → Connectors** and ask in a normal chat. Claude on the web uses a remote custom connector with a reachable, authenticated server. [Claude chat setup](docs/setup.md#use-courselink-in-claude-chat) covers both. Claude Code is an optional coding workflow, not a requirement.
- **“anything new?”** reads cached posts, changed instructions, and due dates immediately. the monitor targets a refresh every 5 minutes while running. routine checks use the cache; a forced scan needs an explicit refresh request. answers include when the data was last checked.
- **“what do i need for this assignment?”** pulls the instructions and searches inside downloaded files.
- **files ready when you sit down.** set up automatic sync and new or changed files land in the right course folders when your computer is online. sync skips identical files already there and keeps older versions when files change. open the folder in Claude Code, Codex, Hermes, or another local assistant to work with the downloaded instructions and starter files, understand the assignment, and get help as you work.

sign in through the separate browser window, pick your courses, and you're set. it saves your session; if CourseLink logs you out, just sign in again. runs locally or on a server, with remote access for supported clients.

built for University of Guelph CourseLink. for setup, clone it and follow the [setup guide](docs/setup.md), or ask a coding assistant: “set this up using [AGENTS.md](AGENTS.md).” ChatGPT needs a supported connection to your monitor; a local MCP client can connect directly on your computer. each person signs into their own CourseLink account and picks their own courses.

made by [@hamza-paracha](https://github.com/hamza-paracha).
