import { neonConfig } from "@neondatabase/serverless";
import { readFile } from "node:fs/promises";
const local = new Set(["127.0.0.1", "localhost", "[::1]"]);
let neonEndpoint: URL | undefined, connection: URL | undefined;
if (
  process.env.ACU_GATE_NEON_TEST === "1" &&
  process.env.ACU_GATE_DATABASE_URL_FILE
) {
  connection = new URL(
    (await readFile(process.env.ACU_GATE_DATABASE_URL_FILE, "utf8")).trim(),
  );
  const endpoint =
    typeof neonConfig.fetchEndpoint === "function"
      ? neonConfig.fetchEndpoint(connection.hostname, 5432)
      : neonConfig.fetchEndpoint;
  neonEndpoint = new URL(endpoint);
}
const original = globalThis.fetch;
globalThis.fetch = (input, init) => {
  const url = new URL(input instanceof Request ? input.url : String(input));
  if (!local.has(url.hostname)) {
    const header = new Headers(init?.headers).get("Neon-Connection-String");
    const supplied = header ? new URL(header) : undefined;
    if (
      !connection ||
      !neonEndpoint ||
      url.href !== neonEndpoint.href ||
      !supplied ||
      ["hostname", "pathname", "username", "password"].some(
        (key) => supplied[key] !== connection[key],
      )
    )
      throw new Error("Test network boundary rejected a nonlocal request");
  }
  return original(input, init);
};
