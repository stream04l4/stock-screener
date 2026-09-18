// sourceMeta —— 数据湖"源"元信息共享模块（v6.1.4 O5：全模块统一色板，防漂移）。
//
// 为什么抽出来：O5 要求 SourcePoolPanel 区块1图例 / 区块2源卡片边框标题色 /
// 堆叠条悬停提示**同源**——同一份 SOURCE_COLORS 常量多处引用。v6.1.3 之前色板
// 硬编码在 SourcePoolPanel.vue 内（单点），O5 新增"图例行 + 中文源名 tooltip"后
// 若再复制一份就漂移。抽到本模块：SourcePoolPanel（卡片/图例/tooltip）+
// 任何新组件（如 O4 探测按钮着色）都从这里 import，单一事实来源。
//
// 色板与 brief 逐字一致（sina蓝/tencent橙/baostock紫/tdx青/adata_f10粉/local灰）；
// 后端 by_source 的 source 值域 = adapter 名 + "local"（本地缓存 legacy 行）。

// 固定色板（brief 逐字）；未知源（legacy 't'/'em_local_static' 等）→ UNKNOWN_COLOR。
export const SOURCE_COLORS = {
  sina: "#2563eb", tencent: "#f97316", baostock: "#8b5cf6",
  tdx: "#06b6d4", adata_f10: "#ec4899", local: "#94a3b8",
};
export const UNKNOWN_COLOR = "#cbd5e1";

// 中文源名（O5：图例 + 悬停提示用；brief 逐字 "● sina 新浪 / …"）。
export const SOURCE_LABELS = {
  sina: "新浪", tencent: "腾讯", baostock: "BaoStock",
  tdx: "通达信", adata_f10: "adata", local: "本地缓存",
};

// 图例行固定顺序（brief 逐字：sina/tencent/baostock/tdx/adata_f10/local）。
export const LEGEND_ORDER = ["sina", "tencent", "baostock", "tdx", "adata_f10", "local"];

// source → {color, label}（未知源 → 灰 + 原值当名，不猜）
export function sourceMeta(src) {
  return {
    color: SOURCE_COLORS[src] || UNKNOWN_COLOR,
    label: SOURCE_LABELS[src] || src,
  };
}
