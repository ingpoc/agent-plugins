import { neon } from "@neondatabase/serverless";
import { readFile } from "node:fs/promises";

export type Cell = string | number | boolean | null;
export type Row = Record<string, Cell>;
export const tables = {
  accounts: [
    "id",
    "provider",
    "handle",
    "x_user_id",
    "verified",
    "verified_type",
    "followers_count",
    "avatar_url",
    "profile_url",
    "wall_opt_in",
    "wall_opt_in_at",
    "created_at",
  ],
  claims: [
    "id",
    "account_id",
    "device_id",
    "device_pubkey",
    "device_name",
    "post_url",
    "post_key",
    "follow_check",
    "post_check",
    "status",
    "reject_reason",
    "show_on_wall",
    "post_source",
    "created_at",
    "expires_at",
  ],
  followers: ["provider", "handle", "x_user_id"],
  follower_meta: ["id", "source", "complete", "updated_at"],
  bindings: [
    "account_id",
    "device_id",
    "device_pubkey",
    "device_name",
    "bound_at",
    "last_rebind_at",
  ],
  unlocks: [
    "account_id",
    "provider",
    "post_ref",
    "handle",
    "display_name",
    "avatar_url",
    "profile_url",
    "created_at",
  ],
  used_posts: ["post_key", "account_id", "created_at"],
  claim_device_limits: ["device_id", "day", "count"],
  ref_clicks: ["ref", "day", "count"],
  download_hits: ["day", "count"],
  revocations: ["account_id", "created_at"],
  moved_devices: ["device_id", "account_id"],
  device_replays: ["device_id", "sig_hash", "expires_at"],
} satisfies Record<string, string[]>;
export type Table = keyof typeof tables;
export type Op =
  | { kind: "insert"; table: Table; row: Row; ignore?: boolean }
  | { kind: "update"; table: Table; where: Row; row: Row; required?: boolean }
  | { kind: "delete"; table: Table; where: Row; required?: boolean }
  | {
      kind: "increment";
      table: "ref_clicks" | "download_hits" | "claim_device_limits";
      where: Row;
    };
export interface Store {
  list(table: Table, where?: Row): Promise<Row[]>;
  atomic(ops: Op[]): Promise<void>;
  purgeExpired(at: string): Promise<void>;
}
export class StoreConflict extends Error {}
const unique: Partial<Record<Table, string[][]>> = {
  accounts: [["id"], ["provider", "handle"]],
  claims: [["id"]],
  followers: [["provider", "handle"]],
  follower_meta: [["id"]],
  bindings: [["account_id"], ["device_id"]],
  unlocks: [["account_id"]],
  used_posts: [["post_key"]],
  claim_device_limits: [["device_id", "day"]],
  ref_clicks: [["ref", "day"]],
  download_hits: [["day"]],
  revocations: [["account_id"]],
  moved_devices: [["device_id"]],
  device_replays: [["device_id", "sig_hash"]],
};
function matches(row: Row, where: Row) {
  return Object.entries(where).every(([k, v]) => row[k] === v);
}
export class MemoryStore implements Store {
  private data = new Map<Table, Row[]>();
  async list(table: Table, where: Row = {}) {
    return structuredClone(
      (this.data.get(table) ?? []).filter((r) => matches(r, where)),
    );
  }
  async purgeExpired(at: string) {
    const claims = this.data.get("claims") ?? [];
    for (const row of claims) {
      if (
        row.status === "pending_verification" &&
        Date.parse(String(row.expires_at)) <= Date.parse(at)
      )
        row.status = "expired";
    }
    this.data.set(
      "device_replays",
      (this.data.get("device_replays") ?? []).filter(
        (row) => Date.parse(String(row.expires_at)) > Date.parse(at),
      ),
    );
  }
  async atomic(ops: Op[]) {
    const copy = structuredClone(this.data);
    try {
      for (const op of ops) {
        const rows = this.data.get(op.table) ?? [];
        this.data.set(op.table, rows);
        if (op.kind === "insert") {
          if (
            (unique[op.table] ?? []).some((keys) =>
              rows.some((r) => keys.every((k) => r[k] === op.row[k])),
            )
          ) {
            if (op.ignore) continue;
            throw new StoreConflict("unique_conflict");
          }
          rows.push(structuredClone(op.row));
        } else if (op.kind === "increment") {
          const row = rows.find((r) => matches(r, op.where));
          if (row) row.count = Number(row.count) + 1;
          else rows.push({ ...op.where, count: 1 });
        } else {
          const found = rows.filter((r) => matches(r, op.where));
          if (op.required && !found.length)
            throw new StoreConflict("concurrent_change");
          if (op.kind === "delete")
            this.data.set(
              op.table,
              rows.filter((r) => !matches(r, op.where)),
            );
          else
            for (const row of found) {
              const next = { ...row, ...op.row };
              if (
                (unique[op.table] ?? []).some((keys) =>
                  rows.some(
                    (r) => r !== row && keys.every((k) => r[k] === next[k]),
                  ),
                )
              )
                throw new StoreConflict("unique_conflict");
              Object.assign(row, op.row);
            }
        }
      }
    } catch (error) {
      this.data = copy;
      throw error;
    }
  }
}
function identifier(table: Table, key: string) {
  if (!tables[table].includes(key)) throw new Error("invalid_column");
  return `"${key}"`;
}
export class NeonStore implements Store {
  private sql: ReturnType<typeof neon>;
  private schema: string;
  constructor(url: string, schema = "public") {
    if (!/^[a-z][a-z0-9_]*$/.test(schema)) throw new Error("invalid_schema");
    this.sql = neon(url);
    this.schema = schema;
  }
  private name(table: Table) {
    if (!Object.hasOwn(tables, table)) throw new Error("invalid_table");
    return `"${this.schema}"."${table}"`;
  }
  private filter(table: Table, where: Row, params: Cell[]) {
    return (
      Object.entries(where)
        .map(([key, value]) => {
          params.push(value);
          return `${identifier(table, key)} IS NOT DISTINCT FROM $${params.length}`;
        })
        .join(" AND ") || "TRUE"
    );
  }
  async list(table: Table, where: Row = {}) {
    const params: Cell[] = [];
    const rows = await this.sql.query(
      `SELECT * FROM ${this.name(table)} WHERE ${this.filter(table, where, params)}`,
      params,
    );
    return rows.map((row) =>
      Object.fromEntries(
        Object.entries(row).map(([k, v]) => [
          k,
          v instanceof Date
            ? v.toISOString()
            : (k.endsWith("_at") || k === "expires_at") && typeof v === "string"
              ? new Date(v).toISOString()
              : v,
        ]),
      ),
    ) as Row[];
  }
  async atomic(ops: Op[]) {
    try {
      await this.sql.transaction(
        ops.map((op) => {
          const params: Cell[] = [];
          let query: string;
          if (op.kind === "insert" || op.kind === "increment") {
            const row =
              op.kind === "insert" ? op.row : { ...op.where, count: 1 };
            const keys = Object.keys(row);
            params.push(...Object.values(row));
            query = `INSERT INTO ${this.name(op.table)} (${keys
              .map((k) => identifier(op.table, k))
              .join(",")}) VALUES (${params
              .map((_, i) => `$${i + 1}`)
              .join(",")})`;
            if (op.kind === "increment")
              query += ` ON CONFLICT (${Object.keys(op.where)
                .map((k) => identifier(op.table, k))
                .join(
                  ",",
                )}) DO UPDATE SET count=${this.name(op.table)}.count+1`;
            else if (op.ignore) query += " ON CONFLICT DO NOTHING";
          } else {
            if (op.kind === "update") {
              const set = Object.entries(op.row)
                .map(([k, v]) => {
                  params.push(v);
                  return `${identifier(op.table, k)}=$${params.length}`;
                })
                .join(",");
              query = `UPDATE ${this.name(op.table)} SET ${set} WHERE ${this.filter(
                op.table,
                op.where,
                params,
              )}`;
            } else
              query = `DELETE FROM ${this.name(op.table)} WHERE ${this.filter(op.table, op.where, params)}`;
            if (op.required)
              query = `WITH changed AS (${query} RETURNING 1) SELECT "${
                this.schema
              }".acu_require(count(*)::integer) FROM changed`;
          }
          return this.sql.query(query, params);
        }),
      );
    } catch (error) {
      if (
        error &&
        typeof error === "object" &&
        "code" in error &&
        ["23505", "P0001", "40001"].includes(String(error.code))
      )
        throw new StoreConflict("concurrent_change");
      throw new Error("database_unavailable");
    }
  }
  async purgeExpired(at: string) {
    await this.sql
      .transaction([
        this.sql.query(
          `UPDATE ${this.name("claims")} SET status='expired' WHERE status='pending_verification' AND expires_at <= $1`,
          [at],
        ),
        this.sql.query(
          `DELETE FROM ${this.name("device_replays")} WHERE expires_at <= $1`,
          [at],
        ),
      ])
      .catch(() => {
        throw new Error("database_unavailable");
      });
  }
  async migrate() {
    try {
      await this.sql.query(`CREATE SCHEMA IF NOT EXISTS "${this.schema}"`);
      const source = await readFile(
        new URL("../migrations/001_init.sql", import.meta.url),
        "utf8",
      );
      await this.sql.transaction([
        this.sql.query(`SET LOCAL search_path TO "${this.schema}"`),
        ...source
          .split("-- statement-break")
          .map((statement) => this.sql.query(statement)),
      ]);
    } catch {
      throw new Error("database_migration_failed");
    }
  }
  async cleanupOwnSchema() {
    if (!this.schema.startsWith("acu_test_"))
      throw new Error("test_schema_required");
    await this.sql
      .query(
        `TRUNCATE ${Object.keys(tables)
          .map((t) => this.name(t as Table))
          .join(",")} CASCADE`,
      )
      .catch(() => {
        throw new Error("test_cleanup_failed");
      });
  }
}
/** databaseUrl resolves DATABASE_URL from env or file. */
export async function databaseUrl(env: NodeJS.ProcessEnv) {
  if (env.ACU_GATE_DATABASE_URL_FILE) {
    const value = (
      await readFile(env.ACU_GATE_DATABASE_URL_FILE, "utf8")
    ).trim();
    if (!value) throw new Error("database_url_file_empty");
    return value;
  }
  return env.DATABASE_URL;
}
