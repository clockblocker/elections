import { resolve } from "node:path";
import { ELECTIONS } from "./constants";

const importer = resolve(import.meta.dir, "import-election.ts");

for (const config of ELECTIONS) {
  const child = Bun.spawn([process.execPath, importer, config.slug], {
    cwd: resolve(import.meta.dir, "../.."),
    env: process.env,
    stdout: "inherit",
    stderr: "inherit"
  });
  const exitCode = await child.exited;
  if (exitCode !== 0) throw new Error(`${config.slug} importer exited with status ${exitCode}`);
}

console.log(`Imported all ${ELECTIONS.length} nationwide election datasets`);
