# -*- coding: utf-8 -*-
import sys, io, socket, datetime as dt
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
socket.setdefaulttimeout(20)
import akshare as ak

def probe(name, fn):
    t0 = dt.datetime.now()
    try:
        df = fn()
        if df is None or len(df) == 0:
            print(f"[EMPTY] {name}")
            return
        print(f"[OK {len(df)} rows {df.shape[1]} cols] {name}")
        print("  cols:", list(df.columns))
    except Exception as e:
        print(f"[ERR] {name}: {type(e).__name__}: {e}")
    print(f"  ({dt.datetime.now()-t0} elapsed)")

probe("stock_us_spot_em(涨跌家数)", lambda: ak.stock_us_spot_em())
probe("stock_hk_spot_em(恒指成分/全部港股)", lambda: ak.stock_hk_spot_em())
