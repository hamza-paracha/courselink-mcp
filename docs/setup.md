# Setup and reference

For agent-assisted setup, ask your coding agent to follow [AGENTS.md](../AGENTS.md). It handles installation, course discovery, and MCP configuration; you sign in and select your courses.

Monitor University of Guelph CourseLink, keep versioned downloads, and search course materials through an MCP client. Run it on your own Mac or Linux machine and sign in with your own account in a dedicated browser window.

The monitor indexes accessible content, assignment instructions, announcements, calendar events, and quiz metadata. It preserves file revisions, records changes, and extracts text from PDFs, Office documents, text files, and ZIP members. It does not submit assignments or start quizzes.

## Quick start

Requires Python 3.11–3.14, [uv](https://docs.astral.sh/uv/), and macOS or Linux. On Windows, use WSL with a graphical browser environment; native Windows is not supported because the CLI uses POSIX file locking.

```sh
git clone <repository-url>
cd courselink-mcp
uv sync --locked
uv run playwright install chromium
uv run courselink init
```

On Linux, install Chromium's system dependencies if needed with `uv run playwright install --with-deps chromium`.

`init` prints the location of your private `config.json`. By default, configuration and browser state live in `~/Library/Application Support/CourseLink MCP`. Set `COURSELINK_STATE_DIR` to choose another location, using the same value for every command and MCP client.

A new installation starts with **no courses selected**. Sign in and discover your courses:

```sh
uv run courselink login
uv run courselink serve
```

Complete sign-in and MFA in the separate Chromium window. Keep `serve` running. In another terminal, use `uv run courselink status` to check authentication and scan progress. Connect your MCP client as shown below and call `list_courses` to get your own course IDs. You can also find an ID in the `/d2l/home/<course-id>` URL when opening a course in CourseLink.

Stop the service, edit the private `config.json`, and add the courses you want monitored. For example, the following uses a **fictional ID**:

```json
{
  "courses": {
    "123456": "example-course"
  }
}
```

Merge this field into the existing configuration; keep its other settings. Folder names may contain letters, digits, hyphens, or underscores. The `school` setting is the download root, initially the `downloads` folder inside your private state directory. Files are saved under `<school>/<course-folder>/CourseLink Downloads/`.

Start `uv run courselink serve` again. The next scan indexes your selected courses and downloads their available files. Existing private configuration is preserved when upgrading.

## Connect an MCP client

Copy the `courselink` entry from [mcp-client.json](../mcp-client.json) into a client that supports MCP stdio, replacing `/absolute/path/to/courselink-mcp` with your clone's absolute path:

```json
{
  "mcpServers": {
    "courselink": {
      "command": "uv",
      "args": [
        "--directory", "/absolute/path/to/courselink-mcp",
        "run", "--locked", "courselink", "stdio"
      ]
    }
  }
}
```

The client must be able to find `uv`; use its absolute executable path if necessary. If you set `COURSELINK_STATE_DIR`, add it to the server entry's `env` object. The stdio command bridges to the service; keep `courselink serve` running separately.

Try asking your client to check for new assignments, find a lab's instructions, search downloaded documents, or list recently updated files. Course IDs and your configured folder names both work as course filters. Check `courselink_status` before relying on cached results.

| Tool | Purpose |
| --- | --- |
| `courselink_status` | Authentication, freshness, scan errors, and index coverage |
| `list_courses` | Discover accessible courses and monitored folder mappings |
| `list_items` | Search and paginate content, assignments, announcements, events, quizzes, and linked files |
| `list_materials` | Find labs or assignments across indexed sources |
| `get_item` | Instructions, dates, availability, and original URL |
| `read_document` | Read extracted text or a specific ZIP member |
| `search_documents` | Search indexed document text |
| `list_changes` | Read discoveries, updates, and files no longer visible |
| `list_file_versions` | List retained revisions with hashes and timestamps |
| `download_file` | Recheck a file and preserve changed bytes |
| `read_file` | Read saved bytes in base64 chunks |
| `check_now` | Queue a fresh scan |
| `open_login` | Open the local browser or explain remote session renewal |

`read_document` rechecks the remote file by default. Use `refresh=False` for cached copies while signed out. Labels are inferred from titles and module paths; use `list_items` and full-text search when a lab or assignment is not grouped as expected. Text extraction has time, memory, and size limits. Inspect the original file for scanned pages, diagrams, layout-sensitive tables, unsupported formats, or truncated text. ZIP contents are read without executing or extracting them onto disk.

## Monitoring and authentication

Defaults are a session check every 60 seconds, a metadata scan every 5 minutes after the previous scan finishes, and same-URL file byte rechecks every hour. `download_file` forces a recheck. SHA-256 detects replaced bytes, and earlier versions remain available. The first scan is a discovery baseline, not evidence that the instructor just uploaded those files.

A valid session can be kept active, but institution policy, forced sign-out, MFA, and revoked sessions can still require manual login. Use `open_login` while the local service is running, or stop the service and run `courselink login` again. The browser profile has one owner at a time. Cached data remains readable after authentication expires; new scans and downloads require sign-in. The service host must stay online for continuous monitoring.

The implementation is restricted to Guelph CourseLink. It discovers available Brightspace API versions at runtime. External services are indexed as links and are not crawled.

## Private data

Sign-in happens in the dedicated browser. The exported session file is limited to the CourseLink domain; the persistent browser profile can also contain SSO state and must remain private. Configuration, cookies, bearer tokens, the catalog, downloads, logs, and reports belong on your own machine and must not be committed or shared. The repository contains code and synthetic test fixtures.

The state directory is protected with owner-only permissions. The service binds to `127.0.0.1:8765` by default and requires a bearer token. Do not expose it directly to the internet. MCP tools return private course data to your chosen client; connect only clients you trust. Course content is untrusted source material for the client.

`.gitignore` excludes common runtime data and secrets as a safeguard. If you choose custom download or state paths, keep them outside the clone or add those paths to your local ignore rules.

## Optional Linux service

Clone into `~/courselink-mcp`, install dependencies and Chromium, then initialize and sign in using the intended server state location:

```sh
export COURSELINK_STATE_DIR="$HOME/.local/share/courselink-mcp"
uv run courselink init
uv run courselink login
```

Initial sign-in needs a graphical environment. For a headless server, use the remote login helper below from a computer with a browser. Set `"headless": true` in the server's private configuration before starting the service.

[deploy/courselink.service](../deploy/courselink.service) is a user service template using the paths above. Adjust it for other installation paths, then install it:

```sh
mkdir -p ~/.config/systemd/user
cp deploy/courselink.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now courselink.service
journalctl --user -u courselink.service -n 50 --no-pager
```

To continue after logout, enable lingering for your own server account with `loginctl enable-linger` if your host permits it.

Refresh a headless server session from a local clone with Chromium installed:

```sh
uv run python scripts/remote-login.py \
  --host your-ssh-host \
  --state-dir /absolute/server/state/path
```

The server state directory must already exist. The helper signs in locally with a temporary browser profile, sends the CourseLink session over SSH, and restarts the user service. It uses `courselink.service` unless you pass `--service`. Its temporary profile is removed afterward. Neither passwords nor session contents are printed.

To copy remote downloads and metadata into your own local folders, configure the local private course mappings, then provide your server details:

```sh
export COURSELINK_REMOTE_HOST=your-ssh-host
export COURSELINK_REMOTE_STATE_DIR=/absolute/server/state/path
export COURSELINK_REMOTE_EXECUTABLE=/absolute/server/clone/.venv/bin/courselink
uv run courselink sync
```

`courselink computer-stdio` exposes the usual remote tools plus `download_to_computer`, `sync_to_computer`, and `computer_sync_status`. Use that subcommand in your local MCP client configuration and supply the same environment variables. Transfers verify SHA-256 and preserve locally edited files. Server details have no built-in defaults.

For a remote MCP connection, run the server's `courselink stdio` over SSH. Supply your own SSH host, executable path, and `COURSELINK_STATE_DIR`; keep those details in your private client configuration. The bridge uses the token on the server. File paths returned by tools refer to the service host; use `read_file` to retrieve saved bytes.

## HTTP API

The service also provides MCP Streamable HTTP at `/mcp/` and REST endpoints under `/api/`. Every request requires `Authorization: Bearer <server-token>`, using the token from the private state's `server-token` file. Browser-origin requests are rejected.

| Route | Method | Parameters |
| --- | --- | --- |
| `/api/status` | GET | None |
| `/api/courses` | GET | None |
| `/api/items` | GET | `course_id`, `kind`, `query`, `limit`, `offset` |
| `/api/materials` | GET | `course_id`, `material_type`, `limit`, `offset` |
| `/api/document` | POST | `item_id`, optional `archive_member`, `offset`, `length`, `refresh` |
| `/api/search_documents` | GET | `query`, `course_id`, `limit` |
| `/api/item` | GET | `item_id` |
| `/api/changes` | GET | `after`, `limit`, `course_id` |
| `/api/versions` | GET | `item_id` |
| `/api/download` | POST | `item_id` |
| `/api/read_file` | POST | `version_id`, `offset`, `length` |
| `/api/scan` | POST | `{}` |
| `/api/login` | POST | `{}` |
| `/api/files/{version_id}` | GET | Saved binary file |

Read operations also accept POST with JSON. `read_file` returns at most 256 KiB per request. Downloads default to a 100 MiB limit. Login responses and foreign-origin redirects are rejected. Use an SSH tunnel for remote HTTP access.

## Development

```sh
uv sync --locked
uv run pytest -q
```

Tests use synthetic courses and mocked requests; no live account is needed. They cover change tracking, pagination, revision retention, download limits, path safety, login response rejection, request authentication, document extraction, and partial scan failures.

Released under the [MIT License](../LICENSE).
