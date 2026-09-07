# Blender Lab MCP runtime

Source: https://projects.blender.org/lab/blender_mcp
Revision: `4309a39646e644261624bfcd2bca669b343b7621`
Archive SHA-256: `acb68eb4beff27a84ba751931745e62f03ad51b7be50b3a924624153b6c38197`

Copyright 2026 Blender Authors. GPL-3.0-or-later; see
`LICENSES/GPL-3.0-or-later.txt` and retained source headers.

Studio distributes only the MCP runtime and its prompt. Python sources are
unchanged from this revision. The prompt's documentation section was changed
by Unsloth on 2026-09-07 to link to online documentation instead of bundled files.
The API/manual corpus, its three documentation tools and two RST helpers,
maintenance scripts, and Blender add-on are excluded. There is no automatic download.

Studio's launcher forces stdio. Scene tools connect to the add-on on loopback;
approved CLI tools may launch background Blender. Tools can execute Python and
write files with Blender's permissions. The add-on is not a security sandbox.

Install the add-on separately from https://www.blender.org/lab/mcp-server/.
