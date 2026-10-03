# CourseLink MCP

<p align="center"><img src="assets/alfred-courselink.png" alt="Alfred mid-air hammer-smashing the CourseLink logo, with flying files and debris: downloaded." width="640"></p>

got tired of opening CourseLink, clicking through every course, and downloading assignments one by one. so i made this. plug it into Claude Code, Codex, Hermes, or whatever you use that supports MCP and just ask.

- **“anything new?”** checks for new posts, changed instructions, and due dates. the monitor refreshes every 5 minutes while running, so most questions can use the cache and come back quick.
- **“what do i need for this assignment?”** pulls the instructions and searches inside downloaded files.
- **files ready when you sit down.** set up automatic sync and new or changed files land in the right course folders when your computer is online. get on your computer, open the folder, and start working. it skips identical files already there and keeps older versions when files change.

sign in through the separate browser window, pick your courses, and you're set. it saves your session; if CourseLink logs you out, just sign in again. runs locally or on a server, with remote access for supported clients.

built for University of Guelph CourseLink. clone it and tell your tool: “set this up using [AGENTS.md](AGENTS.md).” [setup guide](docs/setup.md) has the details.

made by [@hamza-paracha](https://github.com/hamza-paracha).
