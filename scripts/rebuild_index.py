"""按指定 HNSW 参数重建知识库索引（向量原样搬运，不重算嵌入）。

背景：语料从 2,041 涨到 22,943 后，chromadb 默认 `M=16` 的图连通性不足——
部分向量在不带 where 的全库查询里不可达（实测一条政策文档真实余弦 0.5927，
但全库 top-3000 最高只有 0.5017；12 条探针里 2 条丢了真 top-1）。
用默认参数原样重建索引复现同样结果，说明不是索引损坏；单独调高
`ef_search` 也无效，必须加大 `M`。chromadb 不允许事后修改 HNSW 参数，
只能按新参数重建 collection。

做法：复制正式库到临时目录 → 在临时库里建 `invest_new` 并按新参数灌入
全部向量（`get(include=embeddings)` 读出的原始向量，不经过嵌入模型）→
验证（条数、全库 top-10 不劣于分 kind 过滤、自查询找回自身）→ 删旧建新并
改名 → 换目录，旧目录保留为 `knowledge_db_bak_<时间戳>`。

运行: D:\\python\\python.exe scripts/rebuild_index.py [--dry-run] [--force]
"""
from __future__ import annotations

import argparse
import json
import shutil
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

COLLECTION_NAME = "invest"
TMP_SUFFIX = "_rebuild_tmp"
NEW_NAME = "invest_new"
LOCK_PATH = ROOT / "data" / "eval" / "kb_index.lock.json"

# 实测既能修好召回、耗时也可接受的参数：
#   默认 M=16 → 0/12 泄漏失败（丢真 top-1）；M=32 → 2/3 修好；
#   M=48 + construction_ef=400 + search_ef=1000 → 0/12、36/36 自查询
DEFAULT_M = 48
DEFAULT_EF_CONSTRUCTION = 400
DEFAULT_EF_SEARCH = 1000

PROBES = [
    "货币政策 流动性 央行",
    "安全生产费用提取使用",
    "CPI PPI 通胀",
    "贵州茅台 白酒 营收 净利润",
    "Apple iPhone services revenue",
]
KINDS = ["研报", "宏观新闻", "宏观数据", "新闻", "公告", "股东大会",
         "政策", "披露文件", "论文"]
BATCH = 1000


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _client(path: Path):
    import chromadb
    from chromadb.config import Settings as ChromaSettings

    return chromadb.PersistentClient(
        path=str(path), settings=ChromaSettings(anonymized_telemetry=False))


def _release() -> None:
    """释放 chroma 持有的文件句柄。Windows 下有句柄时目录改名会 WinError 5。"""
    import gc

    try:
        from chromadb.api.shared_system_client import SharedSystemClient

        SharedSystemClient.clear_system_cache()
    except Exception:
        pass
    gc.collect()


def _rename_with_retry(src: Path, dst: Path, tries: int = 10) -> None:
    import time as _t

    for i in range(tries):
        try:
            src.rename(dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise SystemExit(
                    f"目录改名失败（句柄未释放）: {src} → {dst}\n"
                    f"  手动完成: mv '{src.name}' '{dst.name}'")
            _release()
            _t.sleep(0.5 * (i + 1))


def _copy_vectors(src, dst) -> int:
    n = src.count()
    off = 0
    while off < n:
        r = src.get(include=["embeddings", "documents", "metadatas"],
                    limit=BATCH, offset=off)
        if not r["ids"]:
            break
        dst.add(ids=r["ids"], embeddings=r["embeddings"],
                documents=r["documents"], metadatas=r["metadatas"])
        off += len(r["ids"])
    return off


def _verify(col, expect: int) -> list[str]:
    """返回失败项；空列表表示通过。"""
    fails = []
    got = col.count()
    if got != expect:
        fails.append(f"条数不符: {got} != {expect}")

    # 全库 top-10 的最高分不应低于任何单类过滤的最高分——过滤查询走精确比对，
    # 可作为暴力基准；全库近似搜索比它低就说明还有不可达向量。
    for q in PROBES:
        g = max((1 - d for d in col.query(query_texts=[q], n_results=10,
                                         include=["distances"])["distances"][0]),
                default=0.0)
        per_kind = []
        for k in KINDS:
            r = col.query(query_texts=[q], n_results=3, where={"kind": k},
                          include=["distances"])
            if r["distances"] and r["distances"][0]:
                per_kind.append(max(1 - d for d in r["distances"][0]))
        best = max(per_kind, default=0.0)
        if g + 1e-4 < best:
            fails.append(f"全库 top10 低于分类最高分: {q} top10={g:.4f} 分类={best:.4f}")

    # 自查询：文档拿自己的向量应当能找回自己。允许并列——研报里有大量重复切片
    # （同一张表被切成多块），自相似度都是 1.000000，超过 10 条时 k=10 的返回
    # 集合在并列里任取，实测会漏掉其中一条但它在 k=30 里排第 7。判据取「top-10
    # 内，或与第 10 名同分」，真漏掉的仍会被抓出来。
    tot = miss = 0
    for k in KINDS:
        r = col.get(where={"kind": k}, include=["embeddings"], limit=5000)
        ids, embs = r["ids"], r["embeddings"]
        if not ids:
            continue
        for i in range(0, len(ids), max(1, len(ids) // 5)):
            tot += 1
            res = col.query(query_embeddings=[embs[i]], n_results=30,
                            include=["distances"])
            hit = res["ids"][0]
            scores = [1 - d for d in res["distances"][0]]
            if ids[i] not in hit:
                miss += 1
                continue
            rank = hit.index(ids[i])
            if rank >= 10 and scores[rank] + 1e-6 < scores[9]:
                miss += 1
    if miss:
        fails.append(f"自查询未找回自身: {miss}/{tot}")
    print(f"  [verify] 条数={got} 自查询 {tot - miss}/{tot} 命中")
    return fails


def main() -> None:
    ap = argparse.ArgumentParser(description="按新 HNSW 参数重建知识库索引")
    ap.add_argument("--db", default=str(ROOT / "data" / "knowledge_db"))
    ap.add_argument("--m", type=int, default=DEFAULT_M)
    ap.add_argument("--ef-construction", type=int, default=DEFAULT_EF_CONSTRUCTION)
    ap.add_argument("--ef-search", type=int, default=DEFAULT_EF_SEARCH)
    ap.add_argument("--dry-run", action="store_true",
                    help="只复制并重建到临时目录做验证，不替换正式库")
    ap.add_argument("--force", action="store_true", help="临时目录已存在时删除重建")
    args = ap.parse_args()

    from app.rag.embed import get_embedding_function

    db = Path(args.db)
    tmp = db.with_name(db.name + TMP_SUFFIX)
    if not (db / "chroma.sqlite3").exists():
        raise SystemExit(f"不是 Chroma 目录: {db}")
    for port in (8000, 8599):
        if _port_open(port):
            raise SystemExit(f"端口 {port} 有服务在跑，先停后端再重建（会并发写同一个库）")
    if tmp.exists():
        if not args.force:
            raise SystemExit(f"临时目录已存在: {tmp}（加 --force 覆盖）")
        shutil.rmtree(tmp)

    print(f"[1/5] 复制 {db.name} → {tmp.name}")
    shutil.copytree(db, tmp)

    ef = get_embedding_function()
    cli = _client(tmp)
    old = cli.get_collection(COLLECTION_NAME, embedding_function=ef)
    expect = old.count()
    meta = {"hnsw:space": "cosine", "hnsw:M": args.m,
            "hnsw:construction_ef": args.ef_construction,
            "hnsw:search_ef": args.ef_search}
    try:
        cli.delete_collection(NEW_NAME)
    except Exception:
        pass

    print(f"[2/5] 按 M={args.m} construction_ef={args.ef_construction} "
          f"search_ef={args.ef_search} 重建 {expect} 条向量")
    t0 = time.perf_counter()
    new = cli.get_or_create_collection(NEW_NAME, embedding_function=ef, metadata=meta)
    copied = _copy_vectors(old, new)
    build_s = time.perf_counter() - t0
    print(f"  灌入 {copied} 条，用时 {build_s:.1f}s，新库 count={new.count()}")

    print("[3/5] 验证新索引")
    fails = _verify(new, expect)
    if fails:
        for f in fails:
            print(f"  [FAIL] {f}")
        raise SystemExit("验证未通过，正式库未改动")

    print("[4/5] 替换 collection")
    cli.delete_collection(COLLECTION_NAME)
    try:
        new.modify(name=COLLECTION_NAME)
    except Exception as e:  # 少数版本不允许改名为刚删除的名字，退回复制一遍
        print(f"  改名失败（{e}），改为重新建 collection 并复制")
        final = cli.get_or_create_collection(
            COLLECTION_NAME, embedding_function=ef, metadata=meta)
        _copy_vectors(new, final)
        cli.delete_collection(NEW_NAME)
        final_fails = _verify(final, expect)
        if final_fails:
            raise SystemExit(f"回退复制后验证失败: {final_fails}")

    if args.dry_run:
        print(f"[5/5] --dry-run：新索引已建在 {tmp}，正式库未替换")
        return

    print(f"[5/5] 换目录（旧库留作备份）")
    _release()  # 必须先放开句柄，否则 Windows 拒绝目录改名
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = db.with_name(f"{db.name}_bak_{stamp}")
    db.rename(bak)
    _rename_with_retry(tmp, db)
    if not (db / "chroma.sqlite3").exists():
        raise SystemExit(f"换目录后正式库不完整: {db}（备份仍在 {bak}）")

    lock = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "db": str(db.relative_to(ROOT)),
        "backup": str(bak.relative_to(ROOT)),
        "count": expect,
        "hnsw": {"M": args.m, "construction_ef": args.ef_construction,
                 "search_ef": args.ef_search, "space": "cosine"},
        "build_seconds": round(build_s, 1),
        "verified": {"count": expect, "probes": len(PROBES)},
        "note": "HNSW 参数不可事后修改；语料再大幅增长时重跑本脚本。",
    }
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.write_text(json.dumps(lock, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    print(f"  新库: {db}")
    print(f"  备份: {bak}")
    print(f"  锁文件: {LOCK_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
