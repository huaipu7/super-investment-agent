"""数据源修复回归守卫（全离线，不联网、不碰真实缓存库）。

钉住三个会让「无值」变成「坏值」或让「一次网络抖动」变成「一天不可用」的地方：

  1. 净值缺值  上游用 '-' 和空串两种写法表示缺值，只判 '-' 会让 float('') 抛错
  2. 净值取列  东财按日期倒序给两列，取最后一列 = 取昨日，把隔夜值当今日报
  3. SEC 通路  UA 必须声明身份（浏览器 UA 直接 403）；拉取失败不得写缓存

运行: D:\\python\\python.exe scripts/verify_data_sources.py
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    """bool(ok) 是刻意的：断言表达式常以字符串/列表结尾，直接累加会 TypeError。"""
    global passed, failed
    ok = bool(ok)
    passed += ok
    failed += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}" if detail
          else f"  [{'PASS' if ok else 'FAIL'}] {name}")


class _FakeCache:
    """内存缓存替身，避免污染 data/cache.db。"""

    def __init__(self):
        self.store: dict[str, object] = {}

    def get_dict(self, *key):
        return self.store.get(str(key))

    def set_dict(self, value, ttl, *key):
        self.store[str(key)] = value


def _nav_frame(today: str, prev: str) -> pd.DataFrame:
    """复刻东财列序：今日列在前、昨日列在后。"""
    return pd.DataFrame([{
        "基金代码": "005827", "基金简称": "测试基金",
        "2026-11-04-单位净值": today, "2026-11-04-累计净值": today,
        "2026-11-03-单位净值": prev, "2026-11-03-累计净值": prev,
        "日增长值": today, "日增长率": today,
        "申购状态": "开放申购", "赎回状态": "开放赎回", "手续费": "0.15%",
    }])


def main() -> int:
    print("=" * 62)
    print("数据源修复回归守卫")
    print("=" * 62)

    # ── 1. 缺值口径 ──
    print("\n[1] 净值缺值不得抛错")
    from app.data.sources.fund_source import _num

    for raw in ("", "  ", "-", "--", None, "nan", "None", "abc"):
        check(f"{raw!r} → None", _num(raw) is None, repr(_num(raw)))
    check("NaN → None（不能当值往下传）", _num(float("nan")) is None)
    check("bool 不算数值", _num(True) is None)
    check("'1.23' → 1.23", _num("1.23") == 1.23)
    check("数字原样返回", _num(5) == 5.0 and _num(1.5) == 1.5)

    # ── 2. 取最新非空列 ──
    print("\n[2] 今日净值已发布时不得报昨日值")
    import app.data.sources.fund_source as F

    def _nav_with(frame: pd.DataFrame, code: str = "005827"):
        orig = F._load_daily_nav
        F._load_daily_nav = lambda: frame
        try:
            return F.fund_realtime_nav(code)
        finally:
            F._load_daily_nav = orig

    nav = _nav_with(_nav_frame(today="1.1752", prev="1.0121"))
    check("两列都有值 → 取今日净值", nav["单位净值"] == 1.1752, str(nav["单位净值"]))
    check("净值日期与取到的列一致", nav["净值日期"] == "2026-11-04", str(nav["净值日期"]))

    nav = _nav_with(_nav_frame(today="", prev="1.0121"))
    check("今日为空 → 回退昨日净值（不崩、不为空）",
          nav["单位净值"] == 1.0121, str(nav["单位净值"]))
    check("回退时日期同步回退", nav["净值日期"] == "2026-11-03", str(nav["净值日期"]))

    nav = _nav_with(_nav_frame(today="", prev=""))
    check("两列皆空 → None 而非崩溃/填 0", nav["单位净值"] is None, repr(nav["单位净值"]))
    check("行情快照结构仍完整（其余字段照常）",
          nav["基金代码"] == "005827" and nav["申购状态"] == "开放申购")

    # ── 3. SEC 通路 ──
    print("\n[3] SEC 要求声明身份，且失败不得污染缓存")
    import app.data.sources.fi_http as H

    ua = H.sec_user_agent()
    check("UA 声明应用名而非伪装浏览器",
          "Mozilla" not in ua and "Super-Investment-Agent" in ua, ua)
    check("UA 带联系方式（SEC 明文要求）", "@" in ua, ua)

    seen: dict[str, str] = {}

    def _fake_get(url, timeout=30, **kw):
        seen["ua"] = kw.get("headers", {}).get("User-Agent", "")

        class _R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"0": {"cik_str": 320193, "ticker": "AAPL"}}
        return _R()

    real_get = H.get
    H.get = _fake_get
    try:
        H.get_json("https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json")
    finally:
        H.get = real_get
    check("get_json 默认带 SEC 声明性 UA", seen.get("ua") == ua, seen.get("ua", ""))

    from app.data.sources.edgar_source import EdgarSource

    src = EdgarSource()
    src.cache = _FakeCache()
    src._load_cik_map()
    check("成功拉取后缓存已写入", len(src.cache.store) == 1, str(list(src.cache.store)))
    check("ticker→CIK 补零到 10 位",
          src._load_cik_map().get("AAPL") == "0000320193",
          str(src._load_cik_map().get("AAPL")))

    for label, fake in (
        ("抛异常", lambda url: (_ for _ in ()).throw(RuntimeError("403"))),
        ("返回空映射", lambda url: {}),
    ):
        src = EdgarSource()
        src.cache = _FakeCache()
        real_json = None
        import app.data.sources.edgar_source as E
        real_json = E.get_json
        E.get_json = fake
        try:
            try:
                src._load_cik_map()
                raised = False
            except Exception:
                raised = True
        finally:
            E.get_json = real_json
        check(f"{label} → 明确报错而非静默返回空表", raised)
        check(f"{label} → 不写缓存（否则一次抖动废掉一天）",
              src.cache.store == {}, str(src.cache.store))

    # 已中毒的历史空缓存必须能自愈
    src = EdgarSource()
    src.cache = _FakeCache()
    src.cache.set_dict({}, 86400, "edgar_cik_map")
    import app.data.sources.edgar_source as E
    real_json = E.get_json
    E.get_json = lambda url: {"0": {"cik_str": 320193, "ticker": "AAPL"}}
    try:
        healed = src._load_cik_map()
    finally:
        E.get_json = real_json
    check("缓存里是空表时重新拉取（自愈）", healed.get("AAPL") == "0000320193", str(healed))

    # ── 港股行情兜底：Yahoo 对部分网络是 IP 级限流，港股没别的通路就会整段缺失 ──
    import akshare as ak
    from app.data.router import MarketData
    from app.data.sources.akshare_source import AKShareSource

    _hk_frame = pd.DataFrame({
        "日期": ["2026-11-04", "2026-11-05"],
        "开盘": [397.8, 413.8], "收盘": [401.6, 430.4],
        "最高": [402.2, 431.2], "最低": [391.6, 410.2],
        "成交量": [18500105, 56903938], "成交额": [0, 0],
    })
    real_hk = ak.stock_hk_hist
    seen: list[str] = []

    def _fake_hk_hist(**kw):
        seen.append(kw.get("symbol"))
        return _hk_frame.copy()

    ak.stock_hk_hist = _fake_hk_hist
    try:
        hk = AKShareSource().get_hk_history("0700.HK", "5y")
        check("港股兜底：0700.HK → symbol 补零成 00700", seen == ["00700"], str(seen))
        check("港股兜底：按列名映射（东财列序是 开/收/高/低，按位置取会把收盘当最高）",
              list(hk.columns) == ["Open", "High", "Low", "Close", "Volume"]
              and float(hk["Close"].iloc[-1]) == 430.4, str(list(hk.columns)))
        check("港股兜底：索引为升序真日期",
              hk.index.is_monotonic_increasing
              and str(hk.index[-1].date()) == "2026-11-05")

        class _DeadYF:
            def get_history(self, *a, **k):
                raise RuntimeError("YFRateLimitError: Too Many Requests")

            def fetch_price_history(self, *a, **k):
                raise RuntimeError("YFRateLimitError: Too Many Requests")

        md = MarketData()
        md._yf = _DeadYF()
        df = md.get_history("0700.HK", "5y")
        check("yfinance 被限流 → 港股仍取到行情", not df.empty and len(df) == 2,
              f"rows={len(df)}")
        src = (df.attrs.get("vintage") or {}).get("source")
        check("兜底数据溯源标 akshare，不冒名 yfinance", src == "akshare", str(src))

        class _LiveYF:
            def get_history(self, *a, **k):
                return pd.DataFrame(
                    {"Open": [1.0], "High": [1.0], "Low": [1.0],
                     "Close": [1.0], "Volume": [1.0]},
                    index=pd.to_datetime(["2026-11-05"]))

            def fetch_price_history(self, *a, **k):
                return pd.DataFrame()

        md._yf = _LiveYF()
        df2 = md.get_history("0700.HK", "5y")
        check("yfinance 正常时不被误标成 akshare",
              (df2.attrs.get("vintage") or {}).get("source") == "yfinance",
              str(df2.attrs.get("vintage")))
    finally:
        ak.stock_hk_hist = real_hk

    print("\n" + "=" * 62)
    if failed:
        print(f"结果: {passed} 通过, {failed} 失败")
        return 1
    print(f"结果: {passed}/{passed} 全部通过")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
