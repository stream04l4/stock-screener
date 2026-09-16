// 极简 markdown 渲染（移植 app.js renderMarkdown/inlineMd，不引 marked——零新依赖零回归）。
// 先转义再处理：表格 / #/##/### 标题 / - * 列表 / > 引用 / **加粗** / `code`。
// 输出为结构化节点数组（Vue 渲染），不使用 v-html（安全 + 与旧渲染结果等价）。
<script setup>
import { computed } from "vue";

const props = defineProps({ md: { type: String, default: "" } });

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// inlineMd：先 esc，再 **加粗** → <b>、`code` → <code>（旧渲染器对这两处用 innerHTML，
// 内容已转义，等价于受控的少量标签；此处保留同一语义）。
function inlineMd(s) {
  let h = esc(s);
  h = h.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
  h = h.replace(/`(.+?)`/g, "<code>$1</code>");
  return h;
}

// 解析为块节点：{type: 'table'|'h2'|'h3'|'h4'|'ul'|'quote'|'p', ...}
const blocks = computed(() => {
  const lines = (props.md || "").split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^\s*\|.*\|\s*$/.test(line)) {
      const tblLines = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { tblLines.push(lines[i]); i++; }
      const rows = tblLines
        .filter((l) => !/^\s*\|[\s:|-]+\|\s*$/.test(l))
        .map((l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => inlineMd(c.trim())));
      if (rows.length) out.push({ type: "table", rows });
      continue;
    }
    if (/^###\s/.test(line)) { out.push({ type: "h4", html: inlineMd(line.replace(/^###\s*/, "")) }); i++; continue; }
    if (/^##\s/.test(line)) { out.push({ type: "h3", html: inlineMd(line.replace(/^##\s*/, "")) }); i++; continue; }
    if (/^#\s/.test(line)) { out.push({ type: "h2", html: inlineMd(line.replace(/^#\s*/, "")) }); i++; continue; }
    if (/^\s*[-*]\s/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*]\s/.test(lines[i])) { items.push(inlineMd(lines[i].replace(/^\s*[-*]\s*/, ""))); i++; }
      out.push({ type: "ul", items });
      continue;
    }
    if (/^\s*>/.test(line)) { out.push({ type: "quote", html: inlineMd(line.replace(/^\s*>\s?/, "")) }); i++; continue; }
    if (line.trim() === "") { i++; continue; }
    out.push({ type: "p", html: inlineMd(line) });
    i++;
  }
  return out;
});
</script>

<template>
  <div class="md-view">
    <template v-for="(b, i) in blocks" :key="i">
      <table v-if="b.type === 'table'">
        <tr v-for="(r, ri) in b.rows" :key="ri">
          <template v-for="(c, ci) in r" :key="ci">
            <th v-if="ri === 0" v-html="c"></th>
            <td v-else v-html="c"></td>
          </template>
        </tr>
      </table>
      <h2 v-else-if="b.type === 'h2'" v-html="b.html"></h2>
      <h3 v-else-if="b.type === 'h3'" v-html="b.html"></h3>
      <h4 v-else-if="b.type === 'h4'" v-html="b.html"></h4>
      <ul v-else-if="b.type === 'ul'"><li v-for="(x, xi) in b.items" :key="xi" v-html="x"></li></ul>
      <blockquote v-else-if="b.type === 'quote'" style="color:#6b7487;border-left:3px solid #e3e8f0;margin:6px 0;padding-left:10px" v-html="b.html"></blockquote>
      <p v-else v-html="b.html"></p>
    </template>
  </div>
</template>
