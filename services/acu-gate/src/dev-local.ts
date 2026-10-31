import { mkdir, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";
import { once } from "node:events";
import { generateKeyPairSync } from "node:crypto";
import { createGate } from "./server.ts";
import { MemoryStore, NeonStore, databaseUrl } from "./store.ts";
import { startMocks, closeServer } from "./mock-providers.ts";
import { settings } from "./settings.ts";
/** Starts the local development gate and its mock providers. */
export async function devLocal(
  options: {
    port?: number;
    keyDirectory?: string;
    env?: NodeJS.ProcessEnv;
  } = {},
) {
  const mocks = await startMocks();
  const keys = generateKeyPairSync("ed25519");
  const pub = keys.publicKey.export({ format: "jwk" });
  let gate: ReturnType<typeof createGate> | undefined;
  try {
    const port =
      options.port ??
      Number((options.env ?? process.env).ACU_GATE_DEV_PORT || 8790);
    const env = settings({
      ...(options.env ?? process.env),
      ...mocks.env,
      DEV: "1",
      PUBLIC_BASE_URL: `http://127.0.0.1:${port}`,
      FOLLOW_CHECK_MODE: "user_following",
      ENTITLEMENT_PRIVATE_KEY: keys.privateKey
        .export({ format: "der", type: "pkcs8" })
        .toString("base64"),
    });
    const url = await databaseUrl(env),
      store = url ? new NeonStore(url) : new MemoryStore();
    if (store instanceof NeonStore) await store.migrate();
    gate = createGate(env, store, { pageFetch: mocks.pageFetch });
    gate.server.listen(port, "127.0.0.1");
    await once(gate.server, "listening");
    const address = gate.server.address();
    if (!address || typeof address === "string")
      throw new Error("local_listen_failed");
    gate.env.PUBLIC_BASE_URL = `http://127.0.0.1:${address.port}`;
    const dir = options.keyDirectory ?? join(homedir(), ".config/acu-gate");
    await mkdir(dir, { recursive: true });
    await writeFile(
      join(dir, "dev-public-key.txt"),
      Buffer.from(pub.x!, "base64url").toString("base64") + "\n",
      { mode: 0o600 },
    );
    const current = gate;
    return {
      ...gate,
      mocks,
      base: gate.env.PUBLIC_BASE_URL,
      stop: async () => {
        await closeServer(current.server);
        await mocks.close();
      },
    };
  } catch (error) {
    if (gate) await closeServer(gate.server);
    await mocks.close();
    throw error;
  }
}
if (import.meta.main)
  devLocal()
    .then((local) => {
      const stop = () => {
        void local.stop();
      };
      process.once("SIGINT", stop);
      process.once("SIGTERM", stop);
      process.stdout.write(
        `Local ACU gate: ${local.base} (mock_user=guru_test, no_follow, no_post, expanded)\n`,
      );
    })
    .catch((error) => {
      const detail =
        error &&
        typeof error === "object" &&
        "code" in error &&
        error.code === "EADDRINUSE"
          ? ` (EADDRINUSE: port ${process.env.ACU_GATE_DEV_PORT || 8790} is occupied)`
          : "";
      process.stderr.write(`Local ACU gate failed to start${detail}\n`);
      process.exitCode = 1;
    });
