// Plan §6.7: the initial route's JavaScript must stay within 250 KB gzipped. Walks the Vite
// manifest from the entry through its *static* imports (lazy screens and charts excluded).
import { readFileSync } from "node:fs";
import { gzipSync } from "node:zlib";
import { join } from "node:path";

const LIMIT = 250 * 1024;
const dist = new URL("../dist/", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const manifest = JSON.parse(readFileSync(join(dist, ".vite", "manifest.json"), "utf8"));

const entry = Object.keys(manifest).find((key) => manifest[key].isEntry);
if (!entry) throw new Error("no entry in the Vite manifest");

const seen = new Set();
const visit = (key) => {
  if (seen.has(key)) return;
  seen.add(key);
  for (const child of manifest[key].imports ?? []) visit(child);
};
visit(entry);

let total = 0;
for (const key of seen) {
  const file = manifest[key].file;
  if (!file.endsWith(".js")) continue;
  const size = gzipSync(readFileSync(join(dist, file))).length;
  total += size;
  console.log(`${(size / 1024).toFixed(1).padStart(7)} KB  ${file}`);
}
console.log(`${(total / 1024).toFixed(1).padStart(7)} KB  initial JS (gzip), limit ${LIMIT / 1024} KB`);
if (total > LIMIT) {
  console.error("over the initial-route budget (plan §6.7)");
  process.exit(1);
}
