"""一次性下载重排模型（C0）。

huggingface.co 在本机不可达（连接超时），hf-mirror.com 可达，故默认走镜像。
模型落到标准 HF 缓存目录，之后 sentence_transformers 按名字即可加载，
不需要每次开网。

用法:
  D:\\python\\python.exe scripts/fetch_reranker.py            # 下载 + 自检
  D:\\python\\python.exe scripts/fetch_reranker.py --check    # 只自检（离线）
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

MODEL = "BAAI/bge-reranker-base"
MIRROR = "https://hf-mirror.com"
UPSTREAM = "https://huggingface.co"

# 自检用的一对句：相关组应显著高于不相关组，否则模型加载了也没用
PAIRS = [
    ("茅台提价对毛利率的影响", "贵州茅台宣布上调出厂价，毛利率有望提升"),
    ("茅台提价对毛利率的影响", "苹果公司发布新款手机，搭载自研芯片"),
]


def _reachable(url: str, timeout: int = 12) -> bool:
    import requests

    try:
        return requests.get(url, timeout=timeout).status_code < 500
    except Exception:  # noqa: BLE001
        return False


def _pick_endpoint() -> str:
    """先定终点再 import。huggingface_hub 在 import 时就把 ENDPOINT 读成常量，
    之后再改 os.environ 不生效——第一版就栽在这里：明明写的是镜像，
    实际仍去连 huggingface.co，超时后报的还是镜像的名字。"""
    endpoint = os.environ.get("HF_ENDPOINT", "").strip()
    for ep in ([endpoint] if endpoint else []) + [MIRROR, UPSTREAM]:
        if _reachable(ep):
            print(f"[USE ] HF_ENDPOINT={ep}")
            return ep
        print(f"[SKIP] {ep} 不可达")
    raise SystemExit("所有镜像均不可达，未下载任何文件")


def download() -> str:
    ep = _pick_endpoint()
    os.environ["HF_ENDPOINT"] = ep
    # 大文件走镜像时默认 10s 超时太紧；这三个必须在 import 之前设
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "120")
    os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "30")

    from huggingface_hub import snapshot_download

    started = time.time()
    path = snapshot_download(MODEL, allow_patterns=None)
    print(f"[ OK ] {path}  ({time.time() - started:.1f}s)")
    return path


def size_of(path: str) -> float:
    total = sum(p.stat().st_size for p in Path(path).rglob("*") if p.is_file())
    return total / 1024 / 1024


def check() -> None:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    from sentence_transformers import CrossEncoder

    started = time.time()
    model = CrossEncoder(MODEL)
    load_s = time.time() - started
    scores = model.predict(PAIRS)
    print(f"加载耗时 {load_s:.2f}s")
    for (q, d), s in zip(PAIRS, scores):
        print(f"  {float(s):+.4f}  {d[:32]}")
    rel, rel_neg = float(scores[0]), float(scores[1])
    if rel <= rel_neg:
        print("[FAIL] 相关句得分不高于不相关句，模型不可用")
        sys.exit(1)
    print(f"[ OK ] 相关 {rel:+.4f} > 不相关 {rel_neg:+.4f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只做离线自检，不下载")
    args = ap.parse_args()

    if not args.check:
        path = download()
        print(f"磁盘占用 {size_of(path):.0f} MB")
    check()


if __name__ == "__main__":
    main()
