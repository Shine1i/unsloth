// SPDX-License-Identifier: AGPL-3.0-only
// Copyright 2026-present the Unsloth AI Inc. team. All rights reserved. See /studio/LICENSE.AGPL-3.0

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(new URL("../src/features/chat/blender-mcp-setup.tsx", import.meta.url), "utf8");

test("Blender help uses official guides and generic server setup without a managed runtime", () => {
  assert.match(source, /openLink\("https:\/\/www\.blender\.org\/lab\/mcp-server\/"\)/);
  assert.match(source, /openLink\("https:\/\/projects\.blender\.org\/lab\/blender_mcp\/wiki\/Llama\.cpp"\)/);
  assert.match(source, /http:\/\/127\.0\.0\.1:9191\//);
  assert.match(source, /onClick=\{onAddServer\}/);
  assert.doesNotMatch(source, /useEffect|useState|builtin|fetch\(|downloadBlenderAddon/i);
});
