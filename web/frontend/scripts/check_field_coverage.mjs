// v6.2 O2 覆盖率自查（交付物⑤）：strategy.yaml 每个顶层 section.field 必须有 desc/type = 100%。
// 用与 vitest 守卫相同的最小结构解析器读生产 yaml，import strategyMeta.js 比对 FIELD_DESC/FIELD_TYPE。
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { FIELD_DESC, FIELD_TYPE } from "../src/data/strategyMeta.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "../../..");
const yamlText = fs.readFileSync(path.join(REPO_ROOT, "config", "strategy.yaml"), "utf-8");

// 顶层字段结构：列0 section 头；恰好2空格缩进 = 该 section 顶层字段（嵌套 dict 头也算顶层字段）
const fields = {}; // {section: [field,...]}
let section = null;
for (const line of yamlText.split(/\r?\n/)) {
  if (/^[A-Za-z_]\w*:\s*(#.*)?$/.test(line)) {
    section = line.slice(0, line.indexOf(":"));
    fields[section] = [];
  } else if (section && /^ {2}[A-Za-z_]\w*:/.test(line)) {
    fields[section].push(line.slice(2).split(/[:\s]/)[0]);
  }
}

let total = 0, missingDesc = [], missingType = [];
for (const [sec, fl] of Object.entries(fields)) {
  for (const f of fl) {
    total++;
    if (!FIELD_DESC[`${sec}.${f}`]) missingDesc.push(`${sec}.${f}`);
    if (!(f in FIELD_TYPE)) missingType.push(`${sec}.${f}`);
  }
}

console.log("=== v6.2 O2 FIELD_DESC/FIELD_TYPE 覆盖率自查 ===");
console.log(`strategy.yaml sections        = ${Object.keys(fields).length}`);
console.log(`strategy.yaml 顶层字段数       = ${total}`);
console.log(`FIELD_DESC 条数               = ${Object.keys(FIELD_DESC).length}`);
console.log(`FIELD_TYPE 覆盖字段(裸名)      = ${Object.keys(FIELD_TYPE).length}`);
console.log(`缺 desc 的字段                = ${missingDesc.length ? missingDesc.join(", ") : "(无)"}`);
console.log(`缺 type 的字段                = ${missingType.length ? missingType.join(", ") : "(无)"}`);
const cov = total ? ((total - missingDesc.length) / total) * 100 : 0;
console.log(`desc 覆盖率                   = ${cov.toFixed(1)}% (${total - missingDesc.length}/${total})`);
console.log(missingDesc.length === 0 && missingType.length === 0
  ? "RESULT: PASS — 100% 覆盖（yaml 字段数 vs desc 条数 全对齐）"
  : "RESULT: FAIL");
