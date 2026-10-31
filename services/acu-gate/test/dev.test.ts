import { test } from "node:test";
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { join } from "node:path";
import { writeFileSync, unlinkSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { devLocal } from "../src/dev-local.ts";

const execFileAsync = promisify(execFile);

test("dev-local and gate-admin list", async (t) => {
  const local = await devLocal({ port: 0, keyDirectory: mkdtempSync(join(tmpdir(), "acu-gate-dev-")) });
  t.after(async () => {
    await local.stop();
  });
  assert.equal((await fetch(`${local.base}/healthz`)).status, 200);
  const tokenFile = join(process.cwd(), ".tmp-admin-token");
  writeFileSync(tokenFile, local.env.ADMIN_TOKEN!);
  t.after(() => {
    try {
      unlinkSync(tokenFile);
    } catch {
      /* ignore */
    }
  });
  const { stdout } = await execFileAsync(
    "node",
    [join(process.cwd(), "scripts/gate-admin.mjs"), "--wake", "list", "--base", local.base],
    {
      encoding: "utf8",
      env: { ...process.env, ACU_GATE_ADMIN_TOKEN_FILE: tokenFile },
    },
  );
  assert.match(stdout, /"claims"/);
});
