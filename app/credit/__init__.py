# -*- coding: utf-8 -*-
"""信用评级引擎（Phase 3，JD 职责①）。

从 SEC EDGAR 官方 XBRL 财务 + 可选市场数据出发，计算结构信用风险分
（Altman Z'' / Ohlson O / Merton-KMV 违约距离），并用 LightGBM/MLP 做
reduced-form 违约预测，walk-forward 回测（防 look-ahead）→ AUC/KS。

诚实边界：无商业评级 feed，二分类标签为**结构模型代理标签**（Altman Z'' 风险区，
纯会计口径，可复现），并显式标注非评级机构违约真实标签。
"""
