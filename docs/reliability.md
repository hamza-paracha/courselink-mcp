# Keeping CourseLink online

The Python MCP SDK (FastMCP) handles the MCP protocol. A service manager keeps the browser and monitor running. These are separate jobs: an MCP framework alone does not provide uptime.

## Unattended Linux setup

Install the service from [setup.md](setup.md) first. The optional units in `deploy/systemd/` add a watchdog and daily catalog backups. Adapt **every** `WorkingDirectory`, `ExecStart`, and `COURSELINK_STATE_DIR` to the existing clone and private state location before installing them. Never replace the private config or browser profile.

Copy the four `courselink-watchdog.*` and `courselink-backup.*` files into `~/.config/systemd/user/`, then run:

```sh
systemctl --user daemon-reload
systemctl --user enable --now courselink-watchdog.timer courselink-backup.timer
systemctl --user start courselink-backup.service
systemctl --user list-timers 'courselink-*'
```

Enable user lingering (`loginctl enable-linger`) for operation after logout and at boot. Verify it with `loginctl show-user "$USER" -p Linger`.

If an OpenAI tunnel runs as `courselink-tunnel.service` and its readiness endpoint is `http://127.0.0.1:8080/readyz`, append `--tunnel` to the watchdog's `ExecStart`. Keep tunnel credentials in its existing private environment file; never add them to these templates.

The optional `deploy/systemd/courselink-tunnel.conf` drop-in adds restart limits and process restrictions without replacing tunnel configuration. Install it as `~/.config/systemd/user/courselink-tunnel.service.d/reliability.conf`, run `systemctl --user daemon-reload`, and restart the tunnel. Verify `/readyz` responds successfully afterward.

## What recovers automatically

- systemd restarts a crashed monitor after 15 seconds.
- The watchdog checks once a minute. Three consecutive failed checks trigger a restart, with at most one recovery attempt per service every ten minutes.
- Authenticated `/api/health` checks worker progress. Browser operations and scans have time limits; dead or stalled workers become unhealthy. Worker checks tolerate the configured polling intervals and a scan taking up to 30 minutes.
- An optional tunnel check recovers an unready tunnel after repeated failures. It waits for the backend to be healthy first.
- Login expiry and CourseLink outages are reported separately from process health. They do not by themselves trigger monitor restarts. Saved material stays readable while reauthentication is needed.

For maintenance or intentional shutdown, stop the watchdog first so it does not restart the monitor underneath you:

```sh
systemctl --user stop courselink-watchdog.timer courselink-watchdog.service
systemctl --user stop courselink.service
# Perform maintenance, then:
systemctl --user start courselink.service courselink-watchdog.timer
```

## Backups and recovery

The daily job uses SQLite's online backup API, verifies database integrity, and retains seven daily snapshots under the private state's `backups/` directory. Backups are readable only by your account. A failed backup does not rotate away previous snapshots.

These snapshots protect the catalog from accidental changes or corruption. They **do not** copy downloaded files, configuration, cookies, or tokens, and they are on the same disk. Host/disk loss still requires a separate private off-host backup strategy.

To restore a catalog, stop the watchdog and monitor, preserve the current database and any `-wal`/`-shm` sidecars in a private recovery folder, then copy the chosen snapshot to `catalog.sqlite3` with mode `600`. Keep existing downloads in place: the catalog references their paths. Restart the monitor and watchdog and check status before using the restored data.

Inspect failures with:

```sh
systemctl --user status courselink.service courselink-watchdog.timer courselink-backup.timer
journalctl --user -u courselink-watchdog.service -u courselink-backup.service -n 30 --no-pager
```

A healthy process does not prove fresh course data. Check `courselink status` for authentication, `last_scan.finished_at`, `complete`, and scan errors. These jobs cannot prevent Oracle outages, renew a revoked login, or guarantee permanent availability. A future alert destination can notify you of those failures; no email or messaging integration is configured by these units.

For a thorough check, use `courselink check --verify-files` or the MCP tool `check_now(verify_files=True)`. This queues a scan that rechecks every accessible downloadable file, including files whose bytes changed without a metadata change. Wait for a new completed `last_scan` with `verify_files: true`; the queue response does not mean the scan has finished. `duration_seconds` records actual scan time. Normal scans use the configured file-check interval to avoid repeatedly transferring unchanged files.

Linked-file failures are reported per item while other links continue. A partial linked scan, or failure to refresh its parent metadata/files, preserves previous entries until a complete scan can safely reconcile them. File discovery is restricted to same-host `/content/` and `/shared/` storage, without an extension allowlist. Hidden/locked material, external services, and quiz questions are outside the monitor's scope. Text extraction can be incomplete even when a file was successfully saved; inspect document warnings and index coverage before claiming a complete answer.
