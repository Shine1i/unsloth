// SPDX-License-Identifier: AGPL-3.0-only
// Copyright 2026-present the Unsloth AI Inc. team. All rights reserved. See /studio/LICENSE.AGPL-3.0

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(new URL("../src/features/chat/blender-mcp-setup.tsx", import.meta.url), "utf8");

test("reopening enabled Blender uses backend state, not a fresh consent checkbox", () => {
  assert.match(source, /config && !config\.is_enabled && \([\s\S]*?<Checkbox/);
  assert.match(source, /config && !config\.is_enabled && \([\s\S]*?act\("enable"\)/);
  assert.match(source, /disabled=\{locked \|\| !config\?\.available \|\| !validPort \|\| \(!config\.is_enabled && !consent\)\}[\s\S]*?act\("test"\)/);
  assert.doesNotMatch(source, /localStorage/);
});

test("Blender installation opens the official site instead of fetching a ZIP", () => {
  assert.match(source, /openLink\("https:\/\/www\.blender\.org\/lab\/mcp-server\/"\)/);
  assert.doesNotMatch(source, /downloadBlenderAddon|act\("download"\)/);
});
