# 数据、任务与迁移控制

## 不变约束

原媒体只读；应用数据存于VELVET_DATA。主库、独立来源库、配置、图片、OCR证据与浏览器偏好是不同存储，备份范围不得混写。当前主库备份不含独立来源缓存和图片。

人工字段锁、评分收藏、关系修正和问题确认优先保留。目录排除应贯穿扫描、展示、任务与统计；当前仍有一致性欠账，变更时须查UX-AUDIT的U07。

## 模式变更

1. 写明新增表/字段/默认值、旧值解释和读写消费者，避免与应用版本混淆。
2. 迁移可重复执行，覆盖旧表缺列；恢复旧备份后也须补模式。迁移失败不能静默生成空库。
3. 新结论记录来源、文件版本及更新时间；文件变化后失效，不能把旧证据挂给新文件。
4. 不假定应用降级可直接读新模式。回滚使用旧程序和升级前完整数据快照。
5. 发布前用隔离旧库副本演练，记录结果；不能用真实生产库做破坏性测试。

## 事务、锁与队列

避免跨多个事务读旧记录再无校验写入。查明现有DB_MUTEX和任务锁顺序，不能在持有数据库锁时等待网络/长子进程。恢复需互斥，任务不能在恢复期间提交或写旧状态。

持久任务写明排队/运行/完成/失败/取消/过期的转换、去重键、重试上限、冷却、启动对账及文件变化行为。取消要管理自己启动的进程与子进程，不能结束无关程序。自动暂停与取消语义分开，恢复原有用户开关。

当前任务系统尚未完全统一，不应依靠新增进程内布尔值扩展更多互相独立的后台状态。

## 隐私与备份

测试/日志/诊断/Issue/截图不带真实媒体、私人路径、人物偏好、Token或OCR文字。配置及备份可能含凭据，只留在受控位置。模式扫描只能辅助审查，不能证明无秘密。

公开源码包只来自已提交Git文件。生产重启前核验进程、任务空闲和数据位置；不要只凭端口TIME_WAIT或PID0结束进程。不在运行任务写入时强杀服务。完整恢复流程参考DEPLOYMENT与RELEASING，真实跨系统演练仍待完成。

## alpha.5新增契约

[ADR0001](../adr/0001-alpha5-reliability-and-readonly-plans.md)记录任务、快照、计划、来源归属及数据锁。旧主库恢复须补新表与scrape_jobs.engine列；未知在途引擎进入人工核对。完整离线备份与在线主库备份范围不同，见[操作指南](../ALPHA5-OPERATIONS.md)。

## alpha.6开发中的资源契约

resource_claims是进程内线程和独立连接共享的持久读/写租约，整理与其它媒体工作流以此互斥；organization_leases补充执行器设备占用。任务持有租约期间不在数据库事务中等待网络或媒体子进程。不要在活跃任务运行时调用recover，也不要用时间过期清锁；仅DataGuard独占启动或完全停止其它工作的备份恢复中可对账旧租约。

备份恢复可替换整个数据库，包括旧租约和整理日志，因此HTTP恢复保留RESTORING/POST/任务锁，在新库上重新ensure并对账。恢复数据库不撤销已经发生的物理操作；alpha.6完整回滚流程仍需文件日志核对，不可据此放行未验证的文件恢复。

## alpha.6查询缓存

inventory_query_snapshots保存30分钟内的筛选、盘点ID、设备/规则/建议签名及一致统计；inventory_query_items仅存有序物理标识和历史重复数量。最多16份，淘汰时同步清理条目。inventory_query_revision由位置、建议、偏好和排除表的写触发器递增，防止暖翻页使用过期范围。重复初始化幂等；原盘点文件、元数据和执行清单不因缓存到期删除。缓存恢复不能替代执行日志对账。

## 片库读模型（alpha.6开发后端）

media_relation_revision记录推断输入代数及已完成代数；video路径/编号/画质/编码/内容类型、手工关系状态和稳定关系路径变化才重推断。media_links仍是人工/自动关系权威，缓存不能覆盖manual。

browse_projection只存按文件派生的搜索/展示/质量摘要；browse_dirty由权威表触发器登记。browse_state记录源revision、投影模式和已聚合revision；规则/可见性/质量目标变化重算，普通字段变化重算脏文件，再刷新可复用SQL版本聚合browse_version_rows。构建前复查上下文与关系代数，变化返回409，避免使用旧规则标记新投影完成。这里的DDL、投影和失效不构成物理文件操作。

browse_queries/browse_results保存最多16份、30分钟的固定排序卡片查询，源revision变化即拒绝旧页。与持久任务/批准计划无关；恢复后幂等补模式及触发器。还没有全量故障/升级验收，旧前端没有切换。质量证据和完整资料留在权威表，不进入卡片JSON。

第十一批新增browse_query_members(query_id,card_key,movie_key,version_key,resume_id,title,part_count,size)，用于固定选择/合集展开/随机，不是执行清单。query_id与卡片/预览有索引；与browse_results一起到期/淘汰清理。browse_results仅保存排序卡片身份，当前页才补资料；相同源revision和完整筛选可复用有效范围。缺封面范围还核对缓存ID集合。普通轮询不能以数据目录mtime作为图片版本，SQLite WAL/SHM会触发无效刷新。

启动在开放HTTP前准备查询索引并明确打印等待信息；这只是就绪阶段，不宣称消除大库迁移成本。离线克隆/恢复测试必须执行标准DataGuard/LEASES恢复；克隆包含当时持有的备份租约不能直接作为已对账运行库。浏览查询不代表媒体执行或恢复验证。


### 数字视频ID迁移与恢复水位

video_ids在初始化旧videos表时原子迁移为AUTOINCREMENT，保留现有ID、额外字段、约束、索引、触发器、视图和资产UUID关系。分配水位同时检查表引用、已知本地历史JSON引用及数据目录图片/OCR编号；不读取远端资料中的ID。缓存不可读或历史不可解析会报错停止迁移，不能静默忽略后继续分配。调用时数据目录须独占且无媒体写入。

主库/完整恢复先取得当前水位，在暂存数据库完成迁移才替换当前数据，保留回滚快照。恢复后的新自动ID不得低于恢复前已分配水位。它不修复过去已错误关联的缓存，也不代替物理整理日志对账。完整决定和边界见[ADR0003](../adr/0003-monotonic-video-ids.md)。


### 复制绑定撤销

organization_superseded记录被重新审阅替代的撤销任务及新草稿、原任务；claim/resume须检查，不能靠取消状态阻止旧摘要复用。organization_inverses指向当前草稿/任务，历史任务仍保留。unbind_copy以单个事务提交绑定删除、默认位置/质量时间更新、事件、步骤与任务完成，不更新源/目标文件。校验阶段暂停/取消不提交元数据。新语义与恢复保护见[ADR0004](../adr/0004-copy-binding-undo.md)，不放宽物理日志对账门禁。


### 原位保留操作

organization_plans标记operation=keep，requested_operation供页面恢复选择；普通搬迁预览标记relocate，兼容未带该字段的旧copy/move预览。keep草稿固定source=destination及设备/完整指纹，不依赖目标空间；pending→binding→done，失败needs_binding后重新验证再关联。新原文件成功关联才materialize，已有UUID、video_id、人工字段和默认副本不重建。无需新增业务表。暂停/取消保留已完成关联；恢复后幂等重试。恢复门禁现区分纯资料历史与物理操作，必须遵守末节“恢复文件对账”和ADR0009，完整矩阵仍待验收。


### 受控源归档

organization_archive_roots(path,device_token,created)记录强制排除根；organization_archive_plans(draft_id,original_job_id,archive_job_id,root,device_token)关联原copy、固定草稿和归档任务。archive条目保存archive_guard的副本完整指纹、文件身份、绑定/资产/播放身份；普通approve拒绝此操作。

archive沿用move状态及未完成物理操作对账隔离，不能将暂停/取消视作已恢复。原文件位置非默认时才可归档，绑定后保持默认副本。归档根在执行前保留，即使取消/恢复也不静默移除强制排除。恢复反向计划可核验日志所属源文件而不依赖外部副本健康。根与关联均在主库备份内，但备份恢复不撤销归档，物理文件恢复仍需末节及ADR0009的完整对账。详见[ADR0005](../adr/0005-controlled-source-archive.md)。


### 多来源固定计划

inventory_query_items.ordinal是查询内成员序号，不是永久文件ID。选择必须结合所属snapshot校验；temp选择表避免大量参数的SQLite变量上限，条目不存在则拒绝。已生成计划保存source_devices、逐项device_id/file_snapshot/root及选择摘要，不能后续重选最新盘点。

执行草稿新增可选source_roots/target_roots数组及每项根；路径必须属于清单内根，复制目标不得嵌套任一所选来源。旧单根日志继续用原字符串。反向任务和源归档按每项根处理，不使用跨盘字符串commonpath；归档旧草稿缺每项target_root时仍可批准。完整契约见[ADR0006](../adr/0006-fixed-query-multi-root-plans.md)。


### 文件范围与写入排队

resource_scopes(owner,resource)记录活动claim的文件/目录、资产、视频或设备范围；旧claim缺scope仍按全局处理。resource_waiters(ticket,owner,kind,mode,resources,created)保存不可变范围的稳定写入owner，冲突读者及较晚写者不得插队。领取失败也需提交等待票号；暂停/取消删除票号但不提前释放活动租约。删除claim的触发器清其scope；启动独占recover才清过期运行态，禁止时间到期清锁。

任务范围只取固定批准，不随原始资产进入片库而增加视频ID；读者补资产UUID范围保持冲突。reconcile也保存范围，保留原任务/反向修复豁免。备份恢复仍全局独占和原文件日志门禁；部分工作流仍保守全局，详见[ADR0007](../adr/0007-scoped-leases-writer-admission.md)。


目录归组派生计算在单次refresh最多缓存1024目录的规则/命名结果；逐文件manual/code/source仍单独判断。不跨refresh复用，不新增业务表，不改变资产身份或人工关系权威。


### 物理文件详情的只读边界

storage_file_details不新增业务表或迁移资产；固定成员复用inventory_query_items/file_snapshot，索引入口只解析属于视频ID的位置ID（旧索引占位indexed会在真实位置出现后失效）。查询定位可生成已有查询缓存，但不会更改作品关系、字段锁、偏好或质量结论。当前stat与历史大小/mtime/SHA分开，旧索引无盘点时间不伪造，未记录卷身份不宣称已核验。读取持文件/资产/视频范围租约。

位置默认20/上限50，资源每页50；只返回当前文件字段白名单和当前绑定。History保存文件列表筛选/分页及滚动焦点，非永久服务端偏好，不进入公开发行附件；查询过期仍需重新选择，不能靠History延长批准。


### 统一整理预检

organization_drafts/jobs.payload新增可选preflight（version=1、targets的device/filesystem/required/reserve，或keep.files_unchanged）；进入原不可变摘要，不纳入浮动free。旧日志不改写，无字段也按当前规则在批准/执行重检。步骤details.preflight保存失败代码，响应额外preflight_messages为派生显示，不回写批准。

源已有source_device/source_identity/fingerprint证明不得被新探测覆盖，完整哈希后复核设备/身份，盘点设备在资产登记前再核对。不增加业务表/后台系统。预检的目录名称/属性/权限缓存仅本次调用，后续执行用新结果；不通过清日志绕过历史长路径或未完成操作保护。详见[ADR0008](../adr/0008-organization-preflight.md)。


### 恢复文件对账（开发候选）

restore_reconciliation检查两端organization_jobs/steps/inverses的清单摘要与完整步骤，以卷/文件身份重放位置链并完整校验应用改动的现存文件。无业务表迁移。私有restore-plans/<id>.json存固定计划，restore-audits/<uuid>.json存恢复前的双方日志/文件证明/回滚位置；均可能含私人路径，不进Git。实际恢复重新审计，绑定归档SHA、配置和权威DB签名；运行租约/已知派生缓存不入签名，不能用HTTP布尔值放行。

兼容终止状态的keep/unbind_copy可作为纯资料恢复；物理移动须匹配备份所需位置，通常先确认反向。副本留盘，不能为回滚资料删除副本。遗留暂存/未完日志/文件不可验证仍拒绝。主库先完整备份旧DB，完整恢复保留原目录并携带回执；重复恢复旧快照也不抹去本机回执。数字ID水位规则保持。旧文中“所有已执行步骤均拒绝”为此前门禁，现由本节及ADR0009替代，不表示全部升级/灾难恢复已验收。


第二十三批性能调整不新增业务表/字段，不改变movie/version/part哈希、分段命名或人工优先规则。browse_version_rows最终字段/值保持；中间窗口采用窄行，现有派生行可继续使用，无需通过全库改写来升级。LibraryCore恢复重建保留已注入的轻量身份回调，旧三参数构造仍兼容。


### 分页作品详情与聚合schema2

MovieDetails无业务表/队列/文件写入，复用browse_version_rows和固定可见关系；查询过程保持一致读取，应用HTTP恢复后重新初始化读取实例，旧revision即使SQL计数重复也无效。实例代数不进入备份或整理批准。关系编辑max_files在DB_MUTEX范围内按全部索引成员检查后再加载，不能按当前页或可见数绕过上限。

NULL兼容修复将窗口中的missing/watched/position及聚合中的height/size/duration/watched/position/missing/favorite按原Python零值语义解释，避免选错默认版本/续播。browse_state.projection_schema=2；从1升级只将projected_revision置失效并递增revision，保持原逐文件投影与已有rebuild需求；0/未知模式重建。不是业务schema版本，不改写用户字段、UUID或关系键。测试覆盖复用投影、重算聚合及旧令牌失效；决定见ADR0010。

第二十六批当前投影为schema3：browse_version_rows追加version_mtime（该版本全部可见成员的最大mtime）及version_first_id（该mtime中的最小ID）作为详情排序派生依据，原有列和值保持。单成员直接用该ID，多成员在聚合刷新时计算，避免每次详情分页重复查询。它们不是播放首选或物理文件身份。schema1/2升级均复用逐文件投影，仅重建聚合并递增revision；原有rebuild要求保留，0/未知模式仍全量重建。NULL时间使用SQLite的确定性空值排序；旧全图hierarchy对NULL时间的max限制未在本批修复。回归模拟缺少新列的schema2、无完整presentation的升级、旧revision拒绝及同分排序/排除成员。

第二十七批不变更投影格式/模式：窄project回调输出字段和值保持，复用原Path只减少派生开销，不拿规范化/短路径展开结果替换标题识别输入。临时browse_matches缩为固定范围与排序需要的列，仍保留全体命中关系；未改变选择数量、分页身份、快照缓存键或人工数据。初始/恢复入口显式注入相同project，旧调用者缺省继续使用core.present。

第二十八批质量草稿只在当前详情的内存/DOM中维护，不新增数据库表、自动保存或修改人工质量API。后台重绘可保留当前文件未保存的note/decision；保存响应对应旧值时，后续编辑仍保持待保存。关闭后丢弃；确认移除当前记录成功后清理，迟到的移除结果不能关闭或清理另一文件。不是跨标签页冲突合并或可恢复草稿。

第二十九批不变更业务模式、排除或进度API：排除确认只在成功应用且原详情仍匹配ID/代数时丢弃本机草稿并关闭；取消、失败、陈旧预览保留。播放请求代数仅隔离旧元数据/结束回调，不入备份、UUID或整理计划。结束后重播以DOM ended或已看状态从零开始，不依赖尚未更新的展示缓存。


第三十一批保持schema3：缺失质量记录的摘要只在当前refresh事务按目标规则结果复用，首次仍由FileReview.describe生成；有记录的文件绝不进入该缓存，目标/指纹/人工审阅变化走原dirty/rebuild机制。空标签/演员仍为JSON空数组。TEMP browse_cards只保存当前固定匹配的派生聚合，事务连接关闭即释放，持久browse_results/members和选择过期规则不变。全局汇总窗口缩列不改默认版本排序。


第三十二批不增加业务表/列或改变批准摘要。organization_steps.details.skip保存confirmed_at/group_ordinal，organization_step_events追加skipped事件；step.state仍保留原checkpoint。无skip的旧记录行为保持。processed仅done，skipped另计；有跳过且所有条目已处理时job.state使用cancelled，界面解释为“已结束 · 含跳过”，不是成功done。resume拒绝已处理完的范围。领取后重读决定，移动绑定排除跳过组；反向只涉及未跳过范围。复制清理支持verified暂存且保留skip。主库备份恢复已有JSON字段保真，物理对账保护不因跳过取消；决策和完整消费者见[ADR0011](../adr/0011-explicit-precommit-group-skip.md)。


第三十三批新增storage_root_proofs(device_id TEXT PRIMARY KEY,identity TEXT,file_id TEXT,root TEXT,recorded REAL)。file_id是目录st_ino字符串，配合卷身份使用，有文件系统及删除重用边界；不替代文件SHA或稳定资产UUID。旧库只建空表，重复ensure不伪造历史证明，旧登记重连须人工核对；主库/完整SQLite备份自然包含新表。storage_devices六列保持，登记/重连事务同步证据，新位置保留旧ID/快照，不迁移或覆盖用户媒体。

设备状态、盘点、规划、准备和物理详情复用目录身份规则；盘点途中卷/根改变令新快照interrupted，不替换最后完成/部分快照。重连digest含当前登记/证明/候选身份及最新盘点状态。没有改写已批准payload、media_locations或videos.path；已有任务的有效挂载路径恢复仍是未完P0，见[ADR0012](../adr/0012-recorded-directory-proof-and-reconnection.md)。
