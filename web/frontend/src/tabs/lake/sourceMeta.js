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
//
// v6.1.5 F2：local 色 #94a3b8(slate-400) → #64748b(slate-500)。为什么改：白底上
// slate-400 对比度不足（~2.5:1，低于 WCAG AA 3:1），图例/堆叠条/local 源卡片边框
// 在浅底看不清；slate-500 ~4.76:1 达标。单点改本常量 → 图例、堆叠条、源卡片边框
// 三处同源自动跟随（防漂移）。

// 固定色板（brief 逐字）；未知源（legacy 't'/'em_local_static' 等）→ UNKNOWN_COLOR。
export const SOURCE_COLORS = {
  sina: "#2563eb", tencent: "#f97316", baostock: "#8b5cf6",
  tdx: "#06b6d4", adata_f10: "#ec4899", local: "#64748b",   // v6.1.5 F2：slate-500（白底可读）
};
export const UNKNOWN_COLOR = "#cbd5e1";

// 中文源名（O5：图例 + 悬停提示用；brief 逐字 "● sina 新浪 / …"）。
// 仅列**当前活跃**的 6 个 source 值（与 LEGEND_ORDER 对齐）——legacy 别名走下方
// LEGACY_SOURCE_ALIASES，不混进图例固定顺序。
export const SOURCE_LABELS = {
  sina: "新浪", tencent: "腾讯", baostock: "BaoStock",
  tdx: "通达信", adata_f10: "adata", local: "本地缓存",
};

// v6.1.5 F1：已知 legacy source 别名映射（生产库 DISTINCT source 核对结果 + brief 点名）。
// 为什么单独一张表而非塞进 SOURCE_LABELS：图例行固定顺序只渲染当前 6 源，legacy 值
// 不会出现在图例里；但**堆叠条 title / by_source 数字 tooltip** 会把生产库实际出现过的
// 历史 source 值（v6.1 之前的旧写入）逐字透出——裸显英文不可读。本表把这些已知 legacy
// 值映射成中文（保留可追溯性），未知值再由 sourceMeta() 兜底 "其他·<原值>"。
//
// 核对来源（F1 brief：grep 生产库 SELECT DISTINCT source，read_only 连接）：
//   kline_daily      → sina / tencent          （当前活跃，已在 SOURCE_LABELS）
//   stock_master     → baostock                （当前活跃）
//   valuation_daily  → tencent                 （当前活跃）
//   index_daily      → tencent                 （当前活跃）
//   dividend_events  → em_local_static         （legacy：东财本地静态缓存，分红 T4）
//   factor_snapshot  → lake_factors            （legacy：本地因子派生 T8）
//   brief 点名       → t                       （legacy：早期腾讯源标记）
// 全量实际出现值 = {baostock, em_local_static, lake_factors, sina, tencent} ∪ {t(local)}。
export const LEGACY_SOURCE_ALIASES = {
  t: "腾讯(legacy)",            // brief 点名：早期腾讯源标记（生产库当前无此值，保留可追溯）
  em_local_static: "本地缓存",   // 东财本地静态缓存（dividend_events T4 分红历史行）
  lake_factors: "本地因子",      // 本地因子派生（factor_snapshot T8）
};

// 图例行固定顺序（brief 逐字：sina/tencent/baostock/tdx/adata_f10/local）。
export const LEGEND_ORDER = ["sina", "tencent", "baostock", "tdx", "adata_f10", "local"];

// source → {color, label}（v6.1.5 F1：三级解析，不再裸显英文）
//   1) 当前活跃源 SOURCE_COLORS/SOURCE_LABELS → 固定色 + 中文名；
//   2) 已知 legacy 别名 LEGACY_SOURCE_ALIASES → UNKNOWN_COLOR(灰) + 中文别名（可追溯）；
//   3) 真正未知值 → UNKNOWN_COLOR(灰) + "其他·<原值>"（保留原值，不猜）。
export function sourceMeta(src) {
  if (src in SOURCE_COLORS) {
    return { color: SOURCE_COLORS[src], label: SOURCE_LABELS[src] };
  }
  if (src in LEGACY_SOURCE_ALIASES) {
    return { color: UNKNOWN_COLOR, label: LEGACY_SOURCE_ALIASES[src] };
  }
  return { color: UNKNOWN_COLOR, label: "其他·" + src };
}
