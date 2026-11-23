# -*- coding: utf-8 -*-
"""martingale.central_soe —— 央企识别（v8.1.1，股票池可选收紧门）。

口径：holders_snapshot 最近快照（PIT 版取 ≤ as_of）的 top10 股东中，任一
share_nature='国有股' 的股东名含 CENTRAL_TOKENS 之一（央企集团/央企控股上市
公司/中央金融/国资运营平台）**且** hold_ratio ≥ min_ratio_pct → 该股为
「央企控股」。

用途：universe.central_only=true 时，股票池在既有 SOE 门（双规则 + 控股强度）
之上再取央企集合交集——把「国资」收紧为「央企」（Joel 2026-11-12：行业风控
复杂机制搁置，央国企资质作兜底前置筛选）。

TOKEN 清单（2026-11-12 人工审计定稿，对照湖内 3,996 个 ≥8% 国有 top10 股东名
逐一核验）：
- 覆盖 98 家国务院国资委央企集团 + 中央军委（新兴际华）+ 中央金融（六大行/
  保险/四大 AMC/中央汇金/证金/中信系）+ 国资运营（国新）+ 三大运营商；
- 地方国资（深铁/上海国资/广州/北京/安徽/重庆/江苏 等）**均不在清单**；
- 故意**不用**泛 token「航天工业」（避免把四川航天工业集团=四川地方国资误判
  为央企）——航天系用「航天科技/航天科工/上海航天/贵州航天/航空工业」精确覆盖；
- v8.1.2 招商系补精确 token「招商局/招商证券/招商银行」（招商局集团系子公司
  控股名如招商局轮船/华建/蛇口均含「招商局」；刻意不用泛「招商」，防
  中新建招商/中科招商/招商盈葵等市场化基金误判）；
- 清单只增不删；央企重组产生新集团名（如合并更名）时手工追加并在此留痕。
"""
from __future__ import annotations

from typing import Set

# 央企集团/央企上市母公司/中央金融 名称 token（子串匹配国有股东名）
CENTRAL_TOKENS: tuple = (
    # —— 能源电力 ——
    "中国石油", "中国海油", "中海油", "中国海洋石油", "中国石化", "中国能源投资",
    "国家能源", "华能集团", "大唐集团", "华电集团", "华电煤业", "三峡集团", "国家电网",
    "南方电网", "南网", "鲁能集团", "核工业集团", "中核集团", "中国核建",
    "中广核", "中国广核", "神华集团", "中煤集团", "国家电投",
    # —— 钢铁/建材/军工材料（已剔除地方国企：首钢=北京/柳钢=广西/海钢=海南）——
    "宝武集团", "宝武钢铁", "宝山钢铁", "鞍山钢铁",
    "宝钢集团", "宝钢股份", "中国建材", "新兴际华",
    "中国钢研",
    # —— 铁路/建工/工程 ——
    "中国铁路", "中国中铁", "中国铁建", "中国交建", "中交集团", "中国能建",
    "中国电力建设", "中国电建", "中国能源建设", "中国中车", "中国建筑集团",
    "中国中冶",
    # —— 航空/航天/船舶/电子/兵器（不含泛 token「航天工业」，见模块 docstring）——
    "航空工业", "航天科技", "航天科工", "上海航天", "贵州航天", "中国船舶",
    "中国电子科技", "兵器工业集团", "兵器装备集团", "中兵投资", "中国航空",
    # 中航汉中系（中航成飞 sz.302132 实控链：汉中汉航机电 26.2% + 汉中航空工业 24.1%）
    "汉中汉航",
    # —— 运输/物流/综合央企（已剔除中国重汽=山东重工/山东国资）——
    "招商局集团", "招商局", "招商证券", "招商银行", "招商公路", "保利集团",
    "保利南方", "华侨城集团", "华润", "中信", "中国物流集团", "中国外运长航",
    "中国邮政集团",
    "中国邮政", "中国旅游集团", "远洋集团", "中国远洋", "先正达",
    # —— 中央金融 ——
    "工商银行", "农业银行", "中国银行", "建设银行", "交通银行", "邮储银行",
    "中国人寿", "中国人保", "中国再保险", "中央汇金", "证券金融",
    "中国信达", "中国华融", "中国长城", "中国东方", "中金公司",
    # —— 国资运营/其他央企 ——
    "国新控股", "国新投资", "中国国新", "中国烟草", "国药集团", "中国通用技术",
    "中国蓝星", "武汉邮电科学研究院", "中国化工科学研究院", "中国机械工业集团",
    "中国国机", "中国稀土", "中国有色", "中国黄金集团", "中国铝业", "中国五矿",
    "五矿集团", "中国中化", "中国移动", "中国电信", "中国联通", "国投电力",
    "中国地质科学院", "国家制造业转型升级基金", "中国出版集团", "中国科技出版",
    # —— 2026-11-23 补录（Joel 拍板；全湖审计发现 11-12 清单漏收 8 个央企系，
    # 依据=湖内最大国有股东≥8% 的真实股东名逐一对照 98 家央企集团名）——
    # 中粮（中粮集团有限公司 → 中粮糖业 50.73%/中粮资本 62.78%）/
    # 一汽（中国一汽集团/中国一汽/一汽股权投资 → 一汽解放/启明信息/富奥股份）/
    # 东风汽车（东风汽车集团 → 东风股份 55%/赛力斯 19.65%）/
    # 一重（中国一重集团 → 中国一重 59.88%）/
    # 中国化学工程（集团/股份 → 中国化学 40.64%/东华科技 47.15%）/
    # 中国化工（中化系 2021 合并前旧前缀 → 中化橡胶持风神股份 49.56%；
    # 刻意不用泛「中化」防误伤，「中国中化」前缀名已由既有 token 覆盖）/
    # 国机系（子公司名不含既有「中国国机/中国机械工业集团」：
    # 中国机械工业仪器仪表集团持川仪股份 30.01%/中国机械总院系持哈焊华通 22%）/
    # 中核（子公司名不含既有「中核集团」：中核苏州阀门持中核科技 17.92%）
    "中粮", "一汽", "东风汽车", "一重", "中国化学工程", "中国化工",
    "中国机械工业仪器仪表集团", "中国机械总院", "中核",
)


def _match(name: str) -> bool:
    return any(t in name for t in CENTRAL_TOKENS)


# 数据标注缺口豁免（v8.1.1）：soe_control_overrides 4 只中，仅央企属性的 2 只
# 在 central_only 模式下豁免央企门（控股方为境外法人/境内法人标注，top10 国有
# 股证据缺失但人工已核实为央企：中国海油=中海油集团、四方股份=国网系）。
# 地方国企属性（宇通=郑州国资/潍柴=潍坊国资）不豁免，照常剔除。
CENTRAL_OVERRIDE_CODES: frozenset = frozenset({"sh.600938", "sh.601126"})


def _select_max_state_central(rows, min_ratio_pct: float) -> Set[str]:
    """按每股最大国有股东（hold_ratio 最大）判定央企控股，避免地方国资第一大股东
    + 央企/汇金/证金财务二股东被误放行。"""
    best: dict[str, tuple[float, str]] = {}
    for code, name, ratio in rows:
        r = float(ratio or 0.0)
        nm = str(name or "")
        prev = best.get(code)
        if prev is None or r > prev[0]:
            best[code] = (r, nm)
    return {
        code for code, (r, nm) in best.items()
        if r >= min_ratio_pct and nm and _match(nm)
    }


def central_soe_codes_asof(con, as_of, min_ratio_pct: float) -> Set[str]:
    """PIT 版：≤ as_of 最近快照、top10 最大国有股东名命中央企 token 且占比 ≥
    min_ratio_pct 的 ts_code 集合（动态池回测每月重算用，与
    universe._soe_ratio_map_asof 同快照口径）。"""
    q = """
        SELECT h.ts_code, h.holder_name, h.hold_ratio
        FROM holders_snapshot h
        JOIN (SELECT ts_code, MAX(as_of_date) la
              FROM holders_snapshot WHERE as_of_date <= ?
              GROUP BY ts_code) l
          ON h.ts_code = l.ts_code AND h.as_of_date = l.la
        WHERE h.holder_rank <= 10 AND h.share_nature = '国有股'
    """
    con.execute(q, [str(as_of)[:10]])
    return _select_max_state_central(con.fetchall(), min_ratio_pct)


def central_soe_codes_latest(con, min_ratio_pct: float) -> Set[str]:
    """生产版：全局最新快照（最大国有股东为央企）。"""
    q = """
        SELECT h.ts_code, h.holder_name, h.hold_ratio
        FROM holders_snapshot h
        JOIN (SELECT ts_code, MAX(as_of_date) la
              FROM holders_snapshot GROUP BY ts_code) l
          ON h.ts_code = l.ts_code AND h.as_of_date = l.la
        WHERE h.holder_rank <= 10 AND h.share_nature = '国有股'
    """
    con.execute(q)
    return _select_max_state_central(con.fetchall(), min_ratio_pct)
