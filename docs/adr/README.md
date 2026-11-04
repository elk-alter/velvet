# 架构决策记录

重要取舍写短ADR：数据模型/迁移、队列/锁、公共接口、依赖/模型及隐私边界。普通样式修复不用ADR。ADR记录理由，不自动增加用户审批步骤。

新文件命名NNNN-主题.md；编号递增。状态：提议、采纳、废弃、被替代。保留历史，改变决策时用新记录链接旧记录。不要凭空把既有实现都标为已评审决策。

## 模板

- 标题、日期、状态、关联需求/提交
- 问题与约束
- 候选方案及不采用原因
- 决定和适用范围
- 数据/接口/隐私/运维影响
- 迁移与回滚
- 验证方法及未验证项
- 替代/关联记录

## 已采纳记录

- [0001：alpha.5可靠性与只读计划](0001-alpha5-reliability-and-readonly-plans.md)

- [0003：移除与恢复后不复用数字视频ID](0003-monotonic-video-ids.md)

- [0004：复制绑定撤销并保留文件](0004-copy-binding-undo.md)

- [0005：受控可恢复源归档](0005-controlled-source-archive.md)

- [0006：固定查询与多来源整理计划](0006-fixed-query-multi-root-plans.md)


- [0007：文件范围租约与持久写入排队](0007-scoped-leases-writer-admission.md)


- [0008：统一整理预检与同盘原目录整理](0008-organization-preflight.md)
