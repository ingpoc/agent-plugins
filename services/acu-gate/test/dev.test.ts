import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { devLocal } from "../src/dev-local.ts";
test("dev-local serves health/config and writes only the raw public key", async (t) => {
  const dir = await mkdtemp(join(tmpdir(), "acu-gate-dev-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const local = await devLocal({ port: 0, keyDirectory: dir, env: {} });
  t.after(local.stop);
  assert.equal((await fetch(local.base + "/healthz")).status, 200);
  const response = await fetch(local.base + "/v1/config");
  const config = await response.json();
  assert.equal(config.providers[0], "x");
  assert.equal(config.linkedin.prefill_text, false);
  assert.deepEqual(await readdir(dir), ["dev-public-key.txt"]);
  assert.equal(
    Buffer.from(
      (await readFile(join(dir, "dev-public-key.txt"), "utf8")).trim(),
      "base64",
    ).length,
    32,
  );
});
