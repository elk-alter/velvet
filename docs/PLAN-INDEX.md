# 规划与接力入口

## 唯一来源与冲突处理

|问题|权威文档|
|---|---|
|当前已发布能力|[功能](FEATURES.md)及对应版本说明，仍须核对代码|
|迭代顺序|[路线图](ROADMAP.md)|
|alpha.6总览、必达场景及逐项验收|[8个Feature / A6-01–30](ALPHA6-REQUIREMENTS.md)|
|alpha.5需求及验收|[A5-01–32](ALPHA5-REQUIREMENTS.md)|
|原文件整理Epic/Feature/Story|[E-ORG、ORG-01–35](EPIC-RAW-MEDIA-ORGANIZATION.md)|
|体验问题与后续改进|[U01–40](UX-AUDIT.md)|
|实施状态和证据|[交付台账](DELIVERY-STATUS.md)|
|开发控制|[开发入口](development/README.md)、[仓库约束](../AGENTS.md)|
|当前交接|[交接记录](HANDOFF.md)|
|AI执行指引|[Velvet开发Skill](../skills/velvet-development/SKILL.md)|

需求细节不复制到Skill或台账。冲突时先核对当前用户要求及明确的范围变更，更新权威文档并注明差异；历史发布说明只反映当时交付，不作为当前排期。全部规划仅是设计，不代表完成或授权执行原文件写入。

## 范围

alpha.5：A5-01–32可靠性/目录政策；E-ORG的F01–F05及F09首批。F06复制、F07绑定是条件试点，必须先满足前置验收；F08移动/改名/源处置延后。alpha.6已拆为A6-01–30，硬盘统一管理为必达，旧alpha.5条件/后续分配不能覆盖新的alpha.6承诺；alpha.7方向保留在路线图。

## 使用Skill

仓库保留可版本化副本skills/velvet-development。安装到Codex skills目录后调用 `$velvet-development`；也可在会话直接要求阅读该SKILL.md并按其接力。仅复制Skill不会自动提供仓库，需要可访问的Velvet checkout。安装副本更新时从仓库同步，不能反向成为需求的另一份权威来源。

开发候选证据：[alpha.5进度](ALPHA5-PROGRESS.md) · [验收](ALPHA5-VALIDATION.md) · [运行指南](ALPHA5-OPERATIONS.md) · [性能](ALPHA5-PERFORMANCE.md)。

alpha.6基础发行入口：[操作流程](ALPHA6-OPERATIONS.md) · [整理接口](development/ORGANIZATION-API.md) · [实现与验收缺口](ALPHA6-PROGRESS.md)。

alpha.6升级与受控回滚的可复现证据：[A6-29验收](ALPHA6-UPGRADE-VALIDATION.md)。首发与后续范围分别按需求首发栏及台账跟踪。
