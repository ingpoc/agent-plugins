import { databaseUrl, NeonStore } from "./store.ts";
/** migrate provides its exported service behavior. */
export async function migrate(env = process.env, schema = "public") {
  const url = await databaseUrl(env);
  if (!url) throw new Error("DATABASE_URL is required");
  await new NeonStore(url, schema).migrate();
}
if (import.meta.main)
  migrate().catch(() => {
    process.stderr.write("Migration failed\n");
    process.exitCode = 1;
  });
