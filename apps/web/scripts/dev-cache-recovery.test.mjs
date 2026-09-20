import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  findUnexpectedCurrentMarkers,
  isTurbopackPersistenceFailure,
  recoverTurbopackCache,
  turbopackCacheRoot,
} from "./dev-cache-recovery.mjs";

test("detects duplicate CURRENT markers and clears only generated Turbopack cache", async () => {
  const webRoot = await mkdtemp(join(tmpdir(), "clipforge-web-"));
  const cacheRoot = turbopackCacheRoot(webRoot);
  const versionRoot = join(cacheRoot, "v-test");
  const source = join(webRoot, "src", "keep.tsx");
  await mkdir(versionRoot, { recursive: true });
  await mkdir(join(webRoot, "src"), { recursive: true });
  await writeFile(join(versionRoot, "CURRENT"), "1");
  await writeFile(join(versionRoot, "CURRENT 2"), "2");
  await writeFile(source, "export const keep = true;");

  const markers = await findUnexpectedCurrentMarkers(cacheRoot);
  assert.equal(markers.length, 1);
  assert.match(markers[0], /CURRENT 2$/);

  await recoverTurbopackCache(webRoot);
  assert.deepEqual(await findUnexpectedCurrentMarkers(cacheRoot), []);
  assert.equal(await import("node:fs").then(({ existsSync }) => existsSync(source)), true);
});

test("recognizes only known Turbopack persistence failures", () => {
  assert.equal(isTurbopackPersistenceFailure("Unexpected file in persistence directory: CURRENT 2"), true);
  assert.equal(isTurbopackPersistenceFailure("TypeScript source error"), false);
});
