"""自主深度调查 —— 假设驱动的取证循环。

流程：真实三表 → 确定性信号（signals）→ 成因假设（hypotheses）→
带硬预算的多轮取证（loop，工具见 tools）→ 报告（report）。

设计红线与仓库一致：绝不编造数据。模型只能"指认"证据（给索引），
不能"写"证据（给 URL/正文），见 hypotheses.attribute 与 loop 的校验。
"""
