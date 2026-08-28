import { basename, resolve } from "node:path";
import { db, closeDatabase } from "./db";

const migrationDirectory = resolve(import.meta.dir, "../../db/migrations");
const glob = new Bun.Glob("*.sql");
const files = Array.from(glob.scanSync({ cwd: migrationDirectory })).sort();

await db.unsafe(`
  CREATE TABLE IF NOT EXISTS schema_migrations (
    version text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
  )
`);

for (const file of files) {
  const version = basename(file);
  const applied = await db.unsafe("SELECT 1 FROM schema_migrations WHERE version = $1", [version]);
  if (applied.length) continue;
  const body = await Bun.file(resolve(migrationDirectory, file)).text();
  await db.begin(async (transaction) => {
    await transaction.unsafe(body);
    await transaction.unsafe("INSERT INTO schema_migrations (version) VALUES ($1)", [version]);
  });
  console.log(`Applied ${version}`);
}

await closeDatabase();
