"""T5 fuyao 备源验证（2026-11-27 Joel 定案：去 EM，只验 fuyao，通过则固定主备）。

验证内容：
A. 数据对标：fuyao 现值 vs 湖内 adata_f10 行（同 code+period）
   - roe_weighted ← index_weighted_avg_roe（±0.05pp）
   - yoy_pni      ← calculate_parent_holder_net_profit_yoy_growth_ratio（±1.0pp）
   - gross_margin ← sale_gross_margin（±0.05pp）
   - liability_pct← assets_debt_ratio（±0.05pp）
   - npi          ← net_profit / parent_holder_net_profit（相对 ±1%，判定哪个口径匹配）
B. PIT 检查：
   - fuyao report_date_ms 是否≈公告日（vs 湖内 pub_date）
     ——2026-11-27 结论：= 公告日（非刷新时间戳）；2024Q4 重述期取重述日，
     0 早于湖内 pub_date → 无 lookahead。
   - 重述稳定性：老期（2024Q4 湖内早已灌入）fuyao 现值 vs 湖内值；
     一致 → 无重述漂移 → 备源行可挂主源 pub_date 使用；
     不一致 → 有重述漂移 → 挂主源 pub_date 会引入前视，PIT 不通过。
     2026-11-27 结果：2024Q4 有 vendor 间重述差异（6 股，历史期，备源行
     若使用须以 fuyao 自身 report_date 为 PIT 锚）；2025Q4/2026Q3 一致。

输出：JSON 汇总到 /tmp/fuyao_t5_verify.json + 摘要打印。
"""
import json, time, random, datetime, urllib.request, sys

ENV = {}
for p in ('.env', 'data/.env'):
    try:
        ENV = dict(l.strip().split('=', 1) for l in open(p) if '=' in l and not l.startswith('#'))
        break
    except FileNotFoundError:
        continue
KEY = ENV['HITHINK_FINANCE_API_KEY'].strip()
H = {'X-api-key': KEY, 'User-Agent': 'lake-probe'}
BASE = 'https://fuyao.aicubes.cn/api/a-share'
TZ = datetime.timezone(datetime.timedelta(hours=8))

PERIODS = [('2026Q3', '2026-3'), ('2025Q4', '2025-4'), ('2024Q4', '2024-4')]


def get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=H)
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read())
            if d.get('code') == 0:
                return d
            if d.get('code') in (4001, 5001, 5002, 5003):
                time.sleep(2 * (i + 1))
                continue
            return d  # 业务错误原样返回
        except Exception:
            time.sleep(2 * (i + 1))
    return {'code': -1, 'message': 'network/timeout exhausted'}


def f2v(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def close_pct(a, b, tol):
    """百分比口径：|a-b| <= tol（百分点）"""
    if a is None and b is None:
        return 'both_null'
    if a is None or b is None:
        return 'one_null'
    return 'ok' if abs(a - b) <= tol else f'diff {a - b:+.4f}'


def close_rel(a, b, tol=0.01):
    if a is None and b is None:
        return 'both_null'
    if a is None or b is None:
        return 'one_null'
    if a == 0 and b == 0:
        return 'ok'
    base = abs(a) if abs(a) >= abs(b) else abs(b)
    if base == 0:
        return 'ok' if abs(a - b) < 1e-9 else 'one_null'
    r = abs(a - b) / base
    return 'ok' if r <= tol else f'diff {r * 100:.2f}%'


def to_ths(code):
    """lake 'sh.600546' → fuyao '600546.SH'（已带后缀的原样返回）"""
    if '.' in code and code.split('.', 1)[1] in ('SH', 'SZ', 'BJ'):
        return code
    if '.' in code:
        prefix, num = code.split('.', 1)
        return f'{num}.{prefix.upper()}'
    return code


def main():
    import duckdb
    con = duckdb.connect('data/lake/lake.duckdb', read_only=True)
    # 样本：三只期都有的股票里随机 40 只 + 固定 600546.SH
    codes = [r[0] for r in con.execute("""
        SELECT ts_code FROM fundamentals_quarterly
        WHERE period='2026Q3' AND ts_code IN
          (SELECT ts_code FROM fundamentals_quarterly WHERE period='2025Q4')
        AND ts_code IN (SELECT ts_code FROM fundamentals_quarterly WHERE period='2024Q4')
        ORDER BY hash(ts_code) LIMIT 40
    """).fetchall()]
    if 'sh.600546' not in codes:
        codes.append('sh.600546')
    # 湖内参照值
    ref = {}
    for row in con.execute(f"""
        SELECT ts_code, period, pub_date, roe_weighted, yoy_pni, npi,
               gross_margin, liability_pct, fetched_at
        FROM fundamentals_quarterly
        WHERE ts_code IN ({','.join('?' * len(codes))})
          AND period IN ('2026Q3','2025Q4','2024Q4')
    """, codes).fetchall():
        ref[(row[0], row[1])] = dict(zip(
            ['pub_date', 'roe_weighted', 'yoy_pni', 'npi', 'gross_margin',
             'liability_pct', 'fetched_at'], row[2:]))
    # 湖内各行灌入日期（重述判断用）
    con.close()

    results = []
    stats = {
        'roe': {'ok': 0, 'both_null': 0, 'one_null': 0, 'diff': 0},
        'yoy_pni': {'ok': 0, 'both_null': 0, 'one_null': 0, 'diff': 0},
        'gm': {'ok': 0, 'both_null': 0, 'one_null': 0, 'diff': 0},
        'liab': {'ok': 0, 'both_null': 0, 'one_null': 0, 'diff': 0},
        'npi_parent': {'ok': 0, 'both_null': 0, 'one_null': 0, 'diff': 0},
        'npi_total': {'ok': 0, 'both_null': 0, 'one_null': 0, 'diff': 0},
        'pubdate_eq': 0, 'pubdate_later': 0, 'pubdate_earlier': 0,
        'api_fail': 0,
    }
    t0 = time.time()
    for i, code in enumerate(codes):
        ths = to_ths(code)
        inc = get(f'{BASE}/financials/income-statements?thscode={ths}&period=quarterly&limit=8')
        inc_by_period = {}
        if inc.get('code') == 0:
            for it in inc['data']['item']:
                fy, fp = it['fiscal_year'], it['fiscal_period']
                if fp == 'FY':
                    inc_by_period[f'{fy}Q4'] = it
                else:
                    inc_by_period[f'{fy}Q{fp[1:]}'] = it  # Q3 -> 3
        for lp, fq in PERIODS:
            key = (code, lp)
            if key not in ref:
                continue
            r = ref[key]
            ind = get(f'{BASE}/financials/indicators?thscode={ths}&report={fq}')
            time.sleep(0.3)
            m = {}
            if ind.get('code') == 0:
                for ab in ind['data']['abilities']:
                    for x in ab['indicators']:
                        m[x['index_id']] = f2v(x['value'])
            it = inc_by_period.get(lp)
            rec = {'code': code, 'period': lp,
                   'lake_pub_date': str(r['pub_date']),
                   'lake_fetched_at': str(r['fetched_at'])[:10],
                   'fy_report_date': None, 'fy_npi_parent': None, 'fy_npi_total': None,
                   'fy': m, 'ind_code': ind.get('code')}
            if it:
                rec['fy_report_date'] = datetime.datetime.fromtimestamp(
                    it['report_date_ms'] / 1000, TZ).strftime('%Y-%m-%d')
                rec['fy_npi_parent'] = it.get('parent_holder_net_profit')
                rec['fy_npi_total'] = it.get('net_profit')
            # 对标
            v = {}
            v['roe'] = close_pct(m.get('index_weighted_avg_roe'), r['roe_weighted'], 0.05)
            v['yoy_pni'] = close_pct(m.get('calculate_parent_holder_net_profit_yoy_growth_ratio'), r['yoy_pni'], 1.0)
            v['gm'] = close_pct(m.get('sale_gross_margin'), r['gross_margin'], 0.05)
            v['liab'] = close_pct(m.get('assets_debt_ratio'), r['liability_pct'], 0.05)
            v['npi_parent'] = close_rel(rec['fy_npi_parent'], r['npi'])
            v['npi_total'] = close_rel(rec['fy_npi_total'], r['npi'])
            rec['cmp'] = v
            for k, bucket in (('roe', 'roe'), ('yoy_pni', 'yoy_pni'), ('gm', 'gm'),
                              ('liab', 'liab'), ('npi_parent', 'npi_parent'),
                              ('npi_total', 'npi_total')):
                s = v[k]
                if s in ('ok', 'both_null'):
                    stats[bucket][s] += 1
                elif s == 'one_null':
                    stats[bucket]['one_null'] += 1
                else:
                    stats[bucket]['diff'] += 1
            # pub_date 对比（信息项）
            if rec['fy_report_date'] and r['pub_date']:
                a, b = rec['fy_report_date'], str(r['pub_date'])[:10]
                if a == b:
                    stats['pubdate_eq'] += 1
                elif a > b:
                    stats['pubdate_later'] += 1
                else:
                    stats['pubdate_earlier'] += 1
            if ind.get('code') != 0 or inc.get('code') != 0:
                stats['api_fail'] += 1
            results.append(rec)
        if (i + 1) % 10 == 0:
            print(f'  ...{i + 1}/{len(codes)} codes, {time.time() - t0:.0f}s', flush=True)

    out = {'n_codes': len(codes), 'n_rows': len(results),
           'stats': stats, 'elapsed_s': round(time.time() - t0), 'results': results}
    with open('/tmp/fuyao_t5_verify.json', 'w') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(json.dumps({k: out[k] for k in ('n_codes', 'n_rows', 'stats', 'elapsed_s')},
                     ensure_ascii=False, indent=1))
    # diff 明细（前 20）
    for rec in results:
        diffs = {k: s for k, s in rec['cmp'].items() if s not in ('ok', 'both_null')}
        if diffs:
            print(f"  {rec['code']} {rec['period']}: {diffs}")


if __name__ == '__main__':
    main()
