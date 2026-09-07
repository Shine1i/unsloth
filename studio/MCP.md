# MCP in Unsloth Studio

## Connect Blender MCP

Studio provides setup guidance, not a bundled Blender server or add-on.

1. Open **Manage MCP servers → Set up Blender MCP**. **Download Blender add-on**
   opens [Blender's official MCP page](https://www.blender.org/lab/mcp-server/).
2. In Blender, enable **Preferences → System → Network → Allow Online Access**.
   Drag the website's install button into Blender twice: first to add the Blender
   Lab repository, then to install MCP. After adding the repository, you can also
   search **MCP** in **Get Extensions**.
3. Follow the official [MCP server guide](https://projects.blender.org/lab/blender_mcp/wiki/Llama.cpp)
   to install and start the server locally. Add its URL in Studio (the guide uses
   `http://127.0.0.1:9191/`), test the connection and save the server.
4. Enable MCP for the conversation and use a tool-capable model.

Keep Blender and its add-on running for scene tools. `localhost` refers to the
Studio backend machine, not a remote browser; keep the server and bridge local.
MCP tools can execute Blender Python and write files. Existing tool permissions
apply, and external model providers receive tool results.

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