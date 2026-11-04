# 开发文档入口

本目录定义开发流程；产品状态以 [功能](../FEATURES.md)、[需求](../REQUIREMENTS.md)、[体验审计](../UX-AUDIT.md) 为准，架构现状见 [架构](../ARCHITECTURE.md)。

|文档|用途|
|---|---|
|[工作流程](WORKFLOW.md)|需求、实现、审查、交付与完成条件|
|[测试规范](TESTING.md)|按变更选择验证，不夸大测试结论|
|[模块与接口](MODULES.md)|代码归属、接口边界及扩展约束|
|[数据与迁移](DATA.md)|迁移、并发、任务、恢复及隐私|
|[决策记录](../adr/README.md)|重要设计取舍及模板|
|[发布流程](../RELEASING.md)|版本、归档、附件及回滚|

根目录 [开发约束](../../AGENTS.md) 是执行摘要，[贡献指南](../../CONTRIBUTING.md) 是参与入口。PR模板用于记录证据；它不能自动强制流程。CI目前只有示例，未启用自动合并门禁。

开发依赖：Python3.10+、Git；Node.js用于JS语法与发布检查，不是运行时依赖。采集安装requirements.txt，OCR另安装requirements-vision.txt；媒体检查需FFmpeg/ffprobe。用虚构小片库启动，避免与生产8765端口争用。alpha.5支持VELVET_PORT或--port；测试应选空闲端口及独立VELVET_DATA，参见运行指南。

文档维护：模块变化更新MODULES/ARCHITECTURE，数据变化更新DATA/ADR，用户行为更新FEATURES及对应使用文档，交付范围更新需求和CHANGELOG。历史发行说明不改写成当前状态。

完整规划与接力从 [规划索引](../PLAN-INDEX.md) 开始；AI开发使用 [专用Skill](../../skills/velvet-development/SKILL.md)。
