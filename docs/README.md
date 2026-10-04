# 文档索引

按 raricy_bot 的结构组织。代码与针对性测试决定当前行为；研究结果、产品规则与部署验收各有明确口径。

## 按任务查阅

| 任务 | 文档 |
| --- | --- |
| 项目与架构概览 | [根 README](../README.md) |
| 协作约定 | [AGENTS.md](../AGENTS.md)、[CLAUDE.md](../CLAUDE.md) |
| 开发、配置与测试 | [USAGE.md](usage/USAGE.md) |
| 部署、升级与恢复 | [DEPLOYMENT.md](usage/DEPLOYMENT.md) |
| 投资者申购、分红与赎回 | [GUIDE.md](usage/GUIDE.md) |
| 未认领款人工操作 | [UNCLAIMED_REVIEW.md](usage/UNCLAIMED_REVIEW.md) |
| 站点接口与时间语义 | [SITE_API.md](design/SITE_API.md) |
| 控制用户、机构本金与手续费 | [CONTROL_USER.md](design/CONTROL_USER.md) |
| 人工核对接口与账务约束 | [人工核对设计](design/UNCLAIMED_REVIEW.md) |
| v0.3 基金规则 | [FUND_PLAN_V0.3.md](funds/FUND_PLAN_V0.3.md) |
| 策略、历史效果与图表 | [INTRODUCTION.md](funds/INTRODUCTION.md) |
| 原始研究证据与口径差距 | [研究材料索引](materials/research/README.md) |
| 推广文案 | [promotion/](../promotion/README.md) |
| 历史更新记录 | [归档索引](ARCHIVE.md) |

## 目录职责

- `design/`：仍在维护的设计约束与接口说明。
- `usage/`：开发、运行、部署及用户操作手册；未完成的验收项保留在这里。
- `funds/`：基金规则与研究结论。
- `materials/`：研究证据和图表；原始 CSV、JSON 和图片保留原内容。
- `plans/`：尚未完成的实施计划；当前只保留目录说明。
- `archive/`：已经完成的阶段记录，按日期分目录；仅供追溯。

## 维护规则

变更行为时同步设计与使用说明；历史记录不作为现行操作入口。移动文档时同步根 README、
协作说明、部署物料与推广文案中的链接。根目录保留 README.md、AGENTS.md 与 CLAUDE.md；
推广文案继续放在用户指定的 `/promotion`。
