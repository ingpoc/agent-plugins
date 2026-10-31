import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { settings } from "../src/settings.ts";
import { databaseUrl } from "../src/store.ts";
test("missing production secrets fail loud", () => {
  assert.throws(
    () => settings({ PUBLIC_BASE_URL: "https://gate.example.invalid" }),
    /Missing required setting/,
  );
});
test("empty database URL files cannot silently select MemoryStore", async (t) => {
  const dir = await mkdtemp(join(tmpdir(), "acu-gate-config-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const file = join(dir, "empty.url");
  await writeFile(file, "\n");
  await assert.rejects(
    databaseUrl({ ACU_GATE_DATABASE_URL_FILE: file }),
    /database_url_file_empty/,
  );
});
