# Desktop package lifecycle live tests

These tests exercise installed Tauri packages and destructive backend lifecycle
flows only on disposable Distrobox homes or ephemeral GitHub-hosted runners.
They do not add a test command, mock backend, WebDriver plugin, or special route
to the production application.

Linux and Windows setup screens are driven through `tauri-driver` against the
installed application binary. macOS has no native WKWebView WebDriver, so its
job uses native window input and `screencapture`. The backend web UI is driven
separately with Playwright after the desktop-owned process reports a valid
`/api/health` response.

Every job writes an evidence tree containing:

- `environment.json`;
- exact command output and exit codes under `logs/`;
- DOM and screenshots for user-visible transitions;
- process and filesystem snapshots;
- `results.json`, whose statuses are limited to `verified`, `failed`,
  `not reproducible`, and `blocked`.

The workflow uploads evidence even when a scenario fails. Use
`merge_results.py` after downloading all artifacts to enforce that every P0/P1
scenario has an explicit live disposition before updating
`docs/desktop-install-release-scenario-audit.md`.
