# Claude Code 工作说明

先读[AGENTS.md](AGENTS.md)。用户当前任务、明确分配的文件范围与冻结契约决定工作范围。

## 实施

接手入口：[AGENT_HANDOFF](docs/usage/AGENT_HANDOFF.md)；研究全貌：[ROADMAP](docs/materials/research/ROADMAP.md)；工具与复现：[tools/README](tools/README.md)。

1. 阅读相关代码和交接说明，确认文件所有权。
2. 独立任务可以按用户要求并行；每个执行者只修改分配文件，且知道其他人正在工作。
3. 遇到跨模块契约变化，交给协调者安排调用方与测试更新。不要回滚其他执行者的修改。
4. 保持独立入口 python -m raricy_capital；运行数据与凭据不进入源码和文档。
5. 不修改已冻结策略参数来迎合测试或回测结果，不编造收益、日志或验收结论。

## 针对性验证

minimal targeted verification and tests are enough。

```bash
python -m pytest tests/test_ledger.py -q
python -m pytest tests/test_operations.py -q
python tools/run_capital_service.py --config packaging/funds/config.example.yaml check
```

按实际改动选择命令，不默认扩大到全仓测试。测试运行不了时说明具体原因，协调者可以接手验证；不要把阅读代码当作测试通过。
配置预检可能生成数据目录和管理令牌，但不打开账簿或启动服务。

## 输出

简要说明修改、验证、剩余问题。密码、令牌、Cookie、原始站点错误正文都不进入报告。
只读接口、授权范围内的本机安装和服务运行可以继续；发送消息、转账、交易、发布与提交须遵守当前用户授权。
