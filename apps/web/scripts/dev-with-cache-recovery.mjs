import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";

import {
  findUnexpectedCurrentMarkers,
  isTurbopackPersistenceFailure,
  recoverTurbopackCache,
  turbopackCacheRoot,
} from "./dev-cache-recovery.mjs";

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const nextBin = resolve(webRoot, "..", "..", "node_modules", "next", "dist", "bin", "next");

async function preflight() {
  const markers = await findUnexpectedCurrentMarkers(turbopackCacheRoot(webRoot));
  if (!markers.length) return false;
  await recoverTurbopackCache(webRoot);
  process.stderr.write(
    "ClipForge recovered an invalid generated Turbopack persistence cache before startup.\n",
  );
  return true;
}

function runNextDev() {
  return new Promise((resolveRun) => {
    const child = spawn(process.execPath, [nextBin, "dev"], {
      cwd: webRoot,
      env: process.env,
      stdio: ["inherit", "pipe", "pipe"],
    });
    let recentOutput = "";
    const forward = (stream, target) => {
      stream.on("data", (chunk) => {
        const text = chunk.toString();
        recentOutput = (recentOutput + text).slice(-30_000);
        target.write(chunk);
      });
    };
    forward(child.stdout, process.stdout);
    forward(child.stderr, process.stderr);
    const relay = (signal) => child.kill(signal);
    process.once("SIGINT", relay);
    process.once("SIGTERM", relay);
    child.once("exit", (code, signal) => {
      process.removeListener("SIGINT", relay);
      process.removeListener("SIGTERM", relay);
      resolveRun({ code: code ?? 1, signal, output: recentOutput });
    });
  });
}

await preflight();
let result = await runNextDev();
if (!result.signal && result.code !== 0 && isTurbopackPersistenceFailure(result.output)) {
  process.stderr.write(
    "ClipForge detected a Turbopack persistence failure; clearing only its generated cache and retrying once.\n",
  );
  await recoverTurbopackCache(webRoot);
  result = await runNextDev();
}
process.exitCode = result.code;
