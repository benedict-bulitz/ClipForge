import { readdir, rm } from "node:fs/promises";
import { join, resolve } from "node:path";

const CORRUPT_MARKER = /^CURRENT\s+.+/;

export function turbopackCacheRoot(webRoot) {
  return resolve(webRoot, ".next", "dev", "cache", "turbopack");
}

export async function findUnexpectedCurrentMarkers(cacheRoot) {
  const matches = [];
  let versions;
  try {
    versions = await readdir(cacheRoot, { withFileTypes: true });
  } catch (error) {
    if (error && error.code === "ENOENT") return matches;
    throw error;
  }
  for (const version of versions) {
    if (!version.isDirectory()) continue;
    const directory = join(cacheRoot, version.name);
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      if (entry.isFile() && CORRUPT_MARKER.test(entry.name)) {
        matches.push(join(directory, entry.name));
      }
    }
  }
  return matches;
}

export async function recoverTurbopackCache(webRoot) {
  const expected = turbopackCacheRoot(webRoot);
  const relative = expected.slice(resolve(webRoot).length);
  if (relative !== "/.next/dev/cache/turbopack") {
    throw new Error("Refused to clear an unexpected development-cache path.");
  }
  await rm(expected, { recursive: true, force: true });
}

export function isTurbopackPersistenceFailure(output) {
  return (
    output.includes("Failed to open database") ||
    output.includes("Loading persistence directory failed") ||
    output.includes("Unexpected file in persistence directory")
  );
}
