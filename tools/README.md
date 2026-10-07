# 工具入口：运维与离线量化研究

先读[接手说明](../docs/usage/AGENT_HANDOFF.md)与[研究路线图](../docs/materials/research/ROADMAP.md)。本文记录2026-10-07工具状态，不在阅读或导入时启动基金服务。

## 1. 工具分工

| 文件 | 作用 |
| --- | --- |
| run_capital_service.py | 服务启动薄封装、状态、停机备份与迁移；运行前看USAGE/DEPLOYMENT |
| btc_long_short_research.py | 基础盈亏/强平、小时多空特征、方向与风险预算网格 |
| btc_bear_research.py | 熊市结构、条件空头、参数扩展；依赖基础引擎 |
| btc_bear_control_research.py | 单独匹配名义上限对照；按脚本调用，勿随意import（顶层解析参数） |
| btc_regime_research.py | 波动控制、趋势一致性、状态分类与回放；依赖bear/base |
| btc_regime_diagnostics.py | 前一轮固定候选邻域与单多对照 |
| btc_tier_research.py | 温和/增长两档、回撤预算、空头连亏保护、趋势分歧 |
| btc_tier_diagnostics.py | 六套温和固定方案，完整与近期诊断 |
| btc_long_short_report.py | 第一轮报告生成，显式direction/risk/out参数 |
| btc_regime_report.py / btc_tier_report.py | 后两轮报告生成；当前写入固定2026-10-07目录 |

研究脚本读行情、写离线结果；不导入资金服务、不需要登录或真实站点凭据。直接从项目根 `python tools/脚本.py` 调用，使同级研究模块可导入。

## 2. 环境与轻量验证

标准环境（按需安装，不与运行服务混用）：

```powershell
Set-Location D:\Study\Code\raricy_capital
python -m venv .research-venv
& .\.research-venv\Scripts\python.exe -m pip install -e '.[dev,research]'
& .\.research-venv\Scripts\python.exe -B -m pytest -p no:cacheprovider tests/test_tier_research.py tests/test_regime_research.py tests/test_bear_research.py tests/test_long_short_research.py -q
```

本机此前研究实际用 `C:\Users\yaozi\AppData\Local\Programs\Python\Python313\python.exe`；服务 `.venv` 未安装全部研究库。缺依赖时测试可能被skip，不能将skip记成通过。前一轮四个研究测试文件合计26项通过；文档整理不需要再次回放或扩大测试范围。

## 3. 新一轮回放示例

以下写新目录；运行前确认输出目录不存在，避免覆盖协议与结果。全量数据约7200万行，首次Numba编译与全网格运行需时间。

```powershell
Set-Location D:\Study\Code\raricy_capital
$researchData = 'C:\Users\yaozi\.codex\worktrees\64f2\raricy_bot\artifacts\btc_frozen_2017'
$researchPython = '.\.research-venv\Scripts\python.exe'
& $researchPython tools/btc_tier_research.py --data $researchData --out .build/btc_tier_next
& $researchPython tools/btc_tier_research.py --data $researchData --out .build/btc_tier_turning_next --turning
& $researchPython tools/btc_tier_diagnostics.py --data $researchData --out .build/btc_tier_diagnostics_next --previous .build/btc_tier_next
```

默认28套，turning10套，诊断6套。诊断读取previous的results，跳过已保存完整结果的code；换previous可能改变实际执行数。原版三组为140/72/24次，共236次。

其他参数：基础引擎 `--risk-sweep`；熊市引擎 `--extended` / `--neighbors-only`；状态引擎 `--refined`；均需要 `--data` 与 `--out`。这些不是互相等价的策略开关。

**报告生成器需先改版本化输出与来源映射**：tier/regime报告脚本的OUT和STUDIES写死旧归档和`.build`来源。不要在新研究后直接运行旧生成器覆盖已发表报告。下一轮应同时保存协议、源码快照、结果、图表与manifest，并更新导航。历史参数筛选结果和事后综合建议分别记录。

## 4. 最新NPY字段速查

仅适用于 `btc_tier_research.tier_replay`；其他引擎与早期evidence的字段可能不同。

- daily 8列：time、assets、nav、dd、bound、inputs、cash、reserve。回撤列为比例；报告转为百分数。
- trades 14列：entry_ms、exit_ms、direction、stake、gain、fee、mae_pct、mfe_pct、leverage、nominal/entry_equity、gain/entry_equity、reason、entry_state、target_risk。
- summary 9列：assets、cash、reserve、contributions、nav、max_close_dd、intrabar_bound、halt_ms、liquidations。
- diagnostic 6×4：每种状态bars、exposed_bars、sum_log_NAV_changes、该状态出现时的最大全局DD。

列顺序以当前引擎赋值为准。配置数组有22项，构造位置在runner的run函数；优先使用plans与features，不直接手写脱离协议的裸数组。

## 5. 保存与交付

- `.build/` 是本机中间结果，Git忽略；`docs/materials/research/<版本>/` 是可审查归档。
- 原始3.45GB行情保留旧数据目录，按参数读取，不复制进包或Git。
- `data/`、账簿、凭据、备份属于服务数据，不能作为整理研究的清理目标。
- 工具/依赖与证据目前有未提交改动；提交、推送、发布按用户当前授权处理。
