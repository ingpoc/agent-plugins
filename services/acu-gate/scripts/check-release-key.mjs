#!/usr/bin/env node
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const swift = readFileSync(
  join(
    root,
    "plugins/agent-computer-use/skills/macos-cua/service/Sources/CUAService/IslandGateSecurity.swift",
  ),
  "utf8",
);
if (/productionKey = Data\(repeating: 0, count: 32\)/.test(swift)) {
  process.stderr.write(
    "IslandGateSecurity.swift still uses the all-zero production Ed25519 public key.\n",
  );
  process.exit(1);
}
