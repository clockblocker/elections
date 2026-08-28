import { SQL } from "bun";

const url = process.env.DATABASE_URL ?? "postgres://postgres:postgres@localhost:5432/elections";

export const db = new SQL(url, {
  max: Number(process.env.DATABASE_POOL_SIZE ?? 10),
  idleTimeout: 30,
  connectionTimeout: 10
});

export async function closeDatabase(): Promise<void> {
  await db.close();
}
