"""冻结当前知识库为 L2-v1 只读快照。

外部语料入库后，Chroma 全库 TF-IDF 会重拟合、top-k 会被新文档挤占，
而 l2_labels.json 的 131 个 doc_id 是内容哈希——指标必然漂移。冻结一份
快照后，L2-v1 永远在快照上跑，基线保持可比；新语料另做人工检索样例。

运行: D:\\python\\python.exe scripts\\snapshot_kb.py [--force]
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SRC = ROOT / "data" / "knowledge_db"
DST = ROOT / "data" / "knowledge_db_v1"
LABELS = ROOT / "data" / "eval" / "l2_labels.json"
LOCK = ROOT / "data" / "eval" / "l2_v1.lock.json"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="冻结 knowledge_db 为 L2-v1 快照")
    ap.add_argument("--src", default=str(SRC))
    ap.add_argument("--dst", default=str(DST))
    ap.add_argument("--force", action="store_true",
                    help="目标已存在时覆盖重建（旧快照会被删除）")
    args = ap.parse_args()

    src, dst = Path(args.src), Path(args.dst)
    if not (src / "chroma.sqlite3").exists():
        raise SystemExit(f"源库不存在或不是 Chroma 目录: {src}")
    if not LABELS.exists():
        raise SystemExit(f"缺少人工标注文件: {LABELS}")
    if dst.exists():
        if not args.force:
            raise SystemExit(f"快照已存在: {dst}（要重建请加 --force）")
        shutil.rmtree(dst)

    # 先算源库哈希再复制、复制完立即校验：Chroma 打开时会改写 sqlite 的
    # 内部页，之后再比哈希必然不一致。
    src_sha = _sha256(src / "chroma.sqlite3")
    shutil.copytree(src, dst)
    dst_sha = _sha256(dst / "chroma.sqlite3")
    if src_sha != dst_sha:
        raise SystemExit("快照校验失败：chroma.sqlite3 哈希与源库不一致")

    from app.rag.store import KnowledgeStore

    store = KnowledgeStore(persist_dir=dst)
    count = store.count()

    lock = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "src": str(src.relative_to(ROOT)),
        "dst": str(dst.relative_to(ROOT)),
        "chroma_sha256": dst_sha,
        "count": count,
        "labels": str(LABELS.relative_to(ROOT)),
        "labels_sha256": _sha256(LABELS),
        "note": "L2-v1 只读快照；外部语料不入此库。重建请加 --force。",
    }
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    LOCK.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[snapshot_kb] count={count}  sha256={dst_sha[:16]}…")
    print(f"  快照: {dst}")
    print(f"  锁文件: {LOCK.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
