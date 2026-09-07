# MCP in Unsloth Studio

## Bundled Blender integration

Studio includes the official [Blender Lab MCP](https://projects.blender.org/lab/blender_mcp)
server, **disabled by default**. With Python 3.10+, open **Manage MCP servers**,
find **Blender**, read the execution warning and choose **Enable Blender MCP**.
No commands, Git/uv installs or MCP downloads are needed. Enable MCP for the
conversation and select a tool-capable model.

MCP connectivity and Blender readiness are separate: the bundled documentation
tools work without Blender, while scene tools require a running Blender 5.1+
with its MCP add-on. Use **Test connection** to check both. Studio does not
automatically install or launch Blender or modify its preferences.

### Connecting Blender (optional setup)

1. In Blender, enable **Preferences → System → Network → Allow Online Access**.
2. Click **Download Blender add-on** in Studio to open
   [Blender's official MCP page](https://www.blender.org/lab/mcp-server/).
3. Drag **Drag and Drop into Blender** into Blender twice: first to add the
   Blender Lab repository, then to install MCP. Confirm **Install**.
   After adding the repository, you can also search for **MCP** in **Get Extensions**.
4. Enable the add-on, keep Blender open, then click **Test connection** in Studio.

Use port `9876`, or configure the same port in both apps under advanced settings.

The connection is `Studio → bundled stdio server → 127.0.0.1:9876 → Blender`.
Port 9876 is a TCP bridge, **not an HTTP MCP URL**. No separate HTTP listener or
`UNSLOTH_STUDIO_ENABLE_MCP` setting is needed for this integration.

Blender must be reachable on the **Studio backend machine**, not merely the
browser's machine. Docker, WSL and remote Studio installations may have a
different loopback network; the managed integration does not expose or tunnel
Blender's unauthenticated bridge. Local command execution must also be permitted
by Studio's host policy and configured through an authenticated UI session.

Blender tools can execute Python, modify scenes and write files with Blender's
permissions. The add-on is not a security sandbox. Existing tool permissions
still apply; cancellation cannot undo work already performed. Conversations
share the target Blender scene. With an external model provider, tool results
are sent to that provider.

**Troubleshooting:** check Blender's version, Online Access, enabled MCP add-on,
bridge start state and matching port. A running MCP subprocess alone does not
mean Blender is ready. Disable the integration in Studio to stop using its
tools; stop the bridge or disable the add-on in Blender to stop its listener.
The conversation's MCP switch is separate from the saved server enable state.

Bundled source revision, licenses and archive hashes are recorded in
`backend/vendor/blender_mcp/NOTICE.md` and `PROVENANCE.json`. Install the add-on
from Blender's official page; Studio bundles only the MCP server runtime.

## Studio's own MCP server

Unsloth can expose a local MCP server so an MCP client can inspect models and
GPU state, validate recipes, start or stop training, inspect recipe output, and
export a loaded model.

The server is disabled by default. Enable it for a local Unsloth process with:

```bash
UNSLOTH_STUDIO_ENABLE_MCP=1 \
UNSLOTH_STUDIO_MCP_TOKEN='use-a-local-secret' \
unsloth studio
```

The endpoint is `http://127.0.0.1:8888/mcp/` when Unsloth uses its default port
(a request to `/mcp` redirects to the canonical `/mcp/`). Use the actual Unsloth
port when it is configured differently.

The high-impact tools are:

- `studio_status` and `list_local_models` for discovery
- `get_training_status`, `start_training`, `stop_training`, and `list_training_runs`
- `validate_recipe`, `get_recipe_job_status`, and `get_recipe_job_dataset`
- `load_checkpoint` and `export_gguf`

`start_training` accepts the same fields as the Unsloth `TrainingStartRequest`.
The request is validated by the existing Pydantic model before a subprocess is
started. Export paths use the existing Unsloth validation as well.

The endpoint always requires `UNSLOTH_STUDIO_MCP_TOKEN` and checks an exact
Bearer token for both HTTP and WebSocket connections. Keep it on localhost
unless the deployment has an authenticated reverse proxy. The MCP endpoint is
intentionally opt-in because tools can consume GPU memory, write model
artifacts, and stop active work.