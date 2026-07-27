# Desktop package lifecycle live tests

These tests exercise installed Tauri packages and destructive backend lifecycle
flows only on disposable Distrobox homes or ephemeral GitHub-hosted runners.
They do not add a test command, mock backend, WebDriver plugin, or special route
to the production application.

Linux setup screens are driven through `tauri-driver` against the installed
application binary. Hosted Windows WebView2 did not create a DevTools session
even with an exact runtime/EdgeDriver version match, so Windows and macOS use
minimal native window input plus OS screenshots. The backend web UI is driven
separately with Playwright by launching the freshly installed CLI in normal
HTTP UI mode. The desktop-owned process is intentionally `--api-only`; treating
its expected root 404 as a browser-UI failure would test the wrong contract.

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

`linux_runtime_scenarios.py` additionally composes four real-process runtime
rows in a disposable X11 session: occupied-port fallback (`RUN-01`), an
unrelated listener that must survive (`RUN-02`), adoption after a shell crash
(`RUN-04`), and backend watchdog Retry recovery (`RUN-10`).

The other disposable-Linux probes are deliberately separate because they
mutate different lifecycle boundaries:

- `linux_coexistence_scenarios.py`: partial default root, custom-root-only,
  foreign-root backend, and same-root terminal backend (`COEX-04`, `COEX-05`,
  `COEX-08`, `COEX-09`);
- `linux_package_transition_scenarios.py`: native removal/data preservation,
  deb reinstall, and deb/AppImage switching (`UN-01`, `UN-03`, `UN-04`,
  `UN-06`);
- `linux_fault_scenarios.py`: missing WebKitGTK loader dependency and a
  read-only managed root (`PKG-06`, `INST-10`);
- `linux_window_close_scenarios.py`: native X11 close/reopen while the bundled
  installer is active (`RUN-06`).

Each Linux probe requires `~/.desktop-lifecycle-disposable`; the transition
probe additionally checks the exact deb package name and requires both package
artifacts to live under the disposable home. Representative invocations are:

```bash
xvfb-run -a python3 tests/desktop_lifecycle/linux_runtime_scenarios.py \
  --application /usr/bin/unsloth-studio --evidence "$HOME/runtime-evidence"
xvfb-run -a python3 tests/desktop_lifecycle/linux_coexistence_scenarios.py \
  --application /usr/bin/unsloth-studio --evidence "$HOME/coexistence-evidence"
xvfb-run -a python3 tests/desktop_lifecycle/linux_package_transition_scenarios.py \
  --application /usr/bin/unsloth-studio --deb "$DEB" --appimage "$APPIMAGE" \
  --evidence "$HOME/package-transition-evidence"
xvfb-run -a python3 tests/desktop_lifecycle/linux_fault_scenarios.py \
  --application /usr/bin/unsloth-studio \
  --webkit-link /usr/lib/x86_64-linux-gnu/libwebkit2gtk-4.1.so.0 \
  --evidence "$HOME/fault-evidence"
xvfb-run -a sh -c 'openbox & exec python3 \
  tests/desktop_lifecycle/linux_window_close_scenarios.py \
  --application /usr/bin/unsloth-studio --evidence "$HOME/close-evidence"'
```

The close probe needs `xdotool`, `scrot`, and an X11 window manager such as
Openbox. Production files are never instrumented: the probes observe the
installed binaries, processes, ports, filesystem, logs, screenshots, and UI.
