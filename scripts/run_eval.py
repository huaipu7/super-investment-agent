"""D3 汇总评测 —— 一键跑三项指标并汇总。

  1. eval_intents      意图识别准确率（默认离线确定性路径）
  2. eval_groundedness 报告数值落地性（groundedness）
  3. eval_retrieval    知识库检索 nDCG

运行: D:\\python\\python.exe scripts\\run_eval.py
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> None:
    import eval_groundedness
    import eval_intents
    import eval_retrieval

    r_intent = eval_intents.run(live=False)
    r_ground = eval_groundedness.run()
    r_ret = eval_retrieval.run()

    print("=" * 56)
    print("Super-Investment-Agent 评测汇总（Phase D3）")
    print("=" * 56)
    print(f"[1] 意图识别  mode={r_intent['mode']}  n={r_intent['n']}")
    print(f"    intent_accuracy={r_intent['intent_accuracy']}  "
          f"ticker_accuracy={r_intent['ticker_accuracy']}  "
          f"exact_accuracy={r_intent['exact_accuracy']}")
    if r_intent["failures"]:
        print(f"    失败 {len(r_intent['failures'])} 例")
    print(f"[2] 数值落地   groundedness={r_ground['groundedness']}  "
          f"(n_tokens={r_ground['n_tokens']}, 未归属={r_ground['unattributed']})")
    print(f"    反向控制   fake 99.99% → groundedness={r_ground['control_groundedness']} "
          f"(detected={r_ground['control_detected']})")
    print(f"[3] 检索      mean_ndcg@{r_ret['top_k']}={r_ret['mean_ndcg']} "
          f"(queries={r_ret['queries']})")
    print("=" * 56)

    ok = (r_intent["exact_accuracy"] >= 0.9
          and r_ground["groundedness"] == 1.0 and r_ground["control_detected"]
          and r_ret["mean_ndcg"] >= 0.5)
    print(f"总体: {'PASS' if ok else 'FAIL'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
