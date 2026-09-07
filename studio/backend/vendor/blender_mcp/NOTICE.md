# Official Blender Lab MCP bundle

Source: https://projects.blender.org/lab/blender_mcp
Revision: `4309a39646e644261624bfcd2bca669b343b7621`

The retained upstream files are unmodified. `PROVENANCE.json` records their
SHA-256 hashes and the fetched source archive. Studio supplies the launcher,
not Blender, an interpreter installer, or a model. Python >=3.10 and Blender
>=5.1.0 are required.

## Licenses and attribution

- MCP server, prompts and maintenance scripts: copyright 2026 Blender
  Authors, **GPL-3.0-or-later**, per retained SPDX headers.
- `mcp/blmcp/data/api`: Blender Python API reference, Blender Authors,
  https://docs.blender.org/api/current/. Derived from Blender's GPL source and
  API documentation (https://projects.blender.org/blender/blender, `COPYING`
  and `doc/python_api`). GPL-2.0-or-later; GPL-3.0 redistribution is permitted.
- `mcp/blmcp/data/manual`: Blender Manual by the **Blender Documentation Team**,
  https://docs.blender.org/manual/en/dev/ and
  https://projects.blender.org/blender/documentation, **CC-BY-SA-4.0 or later**,
  except the exclusions recorded in the retained `data/manual/copyright.rst`.
  These excerpts are unchanged from the pinned MCP tree. No upstream icons,
  logos, images or trademarks are bundled. Python examples remain source
  material, not relicensed as CC-BY-SA.

License texts are in `LICENSES/`. Upstream's documentation-copy scripts are
retained as provenance for the API/manual data; they are not run by Studio.

## Operation

The launcher uses stdio only, with no installation or download at launch.
Bundled RST supports local includes; treat the runtime and its data as trusted.

Tools can execute arbitrary Python inside Blender, modify/save files, capture
screenshots, and run the configured Blender executable for background tools.
The add-on's weak sandbox is explicitly not a security boundary. Its TCP bridge
has no authentication: use loopback only and enable it only for trusted clients.

## Blender add-on

Studio bundles only the MCP server, not the Blender add-on. Download and install
it from the official page: https://www.blender.org/lab/mcp-server/.
Enable Blender's Online Access and follow the installation instructions there.
