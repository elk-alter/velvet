# 开发文档入口

开发前检查 Git 工作树、当前运行服务和目标版本。源码变更、公开发行和本机部署各自跟踪。开发与测试使用独立 VELVET_DATA、空闲端口和合成媒体。

|阶段|先读|按影响补读|
|---|---|---|
|需求和排期|[规划索引](../PLAN-INDEX.md)、[alpha.7 需求](../ALPHA7-REQUIREMENTS.md)|[路线图](../ROADMAP.md)、[体验审计](../UX-AUDIT.md)、[交付台账](../DELIVERY-STATUS.md)|
|设计与实现|[仓库约束](../../AGENTS.md)、[开发与交付流程](WORKFLOW.md)|[架构](../ARCHITECTURE.md)、[模块职责](MODULES.md)、[数据与迁移](DATA.md)、[ADR](../adr/README.md)、[片库查询](LIBRARY-QUERY-API.md)、[整理接口](ORGANIZATION-API.md)|
|测试与审查|[测试规范](TESTING.md)、[变更审查模板](../../.github/pull_request_template.md)|[性能基线](../ALPHA6-PERFORMANCE.md)、[升级演练](../ALPHA6-UPGRADE-VALIDATION.md)、[安全说明](../../SECURITY.md)|
|发布与运行|[发布流程](../RELEASING.md)、[部署与回滚](../DEPLOYMENT.md)|[已发布功能](../FEATURES.md)、[用户指南](../USAGE.md)、[硬盘整理操作](../ALPHA6-OPERATIONS.md)|
|开发接力|[当前接力](../HANDOFF.md)、[Velvet 开发 Skill](../../skills/velvet-development/SKILL.md)|[接力模板](../../skills/velvet-development/references/handoff-template.md)|

开发使用 Python 3.10+、Git 和 Node.js。内置刮削及完整测试安装 requirements.txt，OCR 使用可选 requirements-vision.txt，媒体探测与合成播放需要 FFmpeg/ffprobe。Node.js 用于开发检查。CI 配置目前是 [示例](../ci.example.yml)，未启用自动合并门禁。

文档随变更维护。范围与验收更新版本需求，实测结果更新台账并链接证据，接口和设计取舍进入对应模块文档。用户行为变化同步功能与用法，版本历史保留当时结论。贡献入口见 [贡献指南](../../CONTRIBUTING.md)。
