# 整理接口：alpha.6 开发契约

这些接口服务于[硬盘工作区](../ALPHA6-OPERATIONS.md)，尚不是稳定对外API。app.py校验HTTP权限，organization_service协调存储预览、身份、任务与执行器；路由不直接复制或移动文件。

## 访问约束

GET沿用本机Host检查及NAS Basic认证；POST另需X-Velvet-Token及JSON请求体。列表/详情只读取应用已有记录，不接受任意源文件读取。注册目录和计划目标仍由既有存储接口进行受控解析。没有确认标记不能批准执行；摘要必须与服务端固定内容一致。

403表示Token/Host拒绝，401表示NAS认证失败，409表示资源暂不可用，400表示范围/计划/设备/文件等校验失败。页面应显示可操作错误，不将等待误认为成功。执行线程中错误只在统一任务摘要记录类别，具体路径仅在已授权详情中展示。

## 读接口

|方法/路径|参数|返回|
|---|---|---|
|GET /api/organization/preview|id、offset、limit|固定盘点预览的分页items，含index、摘要、范围/空间说明|
|GET /api/organization/draft|id、offset、limit、retained_offset|批准草稿分页、视频/资源计数、完整字节、保留资源分页、inverse_of及原预览id|
|GET /api/organization/jobs|offset、limit|任务摘要分页，不附全量执行清单|
|GET /api/organization/job|id、offset、limit|逐项执行分页、总体进度、paused、反向任务关系|
|GET /api/organization/locations|id（video_id）|已有稳定身份的物理位置；旧索引可做单项离线迁移|

limit上限100，offset为非负；未提供时预览/草稿/详情50，任务列表25。完整计划仍由服务端保存，不能用浏览器当前页代替批准范围。资源分页与视频分页独立。

## 写接口

|方法/路径|关键请求字段|行为|
|---|---|---|
|POST /api/organization/prepare|id（盘点预览）、digest、selected、operation|完整复检并建立不可变执行草稿；operation为copy、move或keep（原位预览）|
|POST /api/organization/approve|id（草稿）、digest、confirmed:true|幂等批准并唤醒队列，返回任务id|
|POST /api/organization/control|id（任务）、action|pause、cancel或resume，控制状态持久化|
|POST /api/organization/undo-preview|id（已停止的移动/复制任务）|移动生成反向文件草稿；复制生成保留文件的绑定撤销草稿；均不执行|
|POST /api/organization/undo-approve|id（原任务）、digest、confirmed:true|批准原任务对应的反向草稿并唤醒队列|
|POST /api/organization/cleanup-preview|id（已停止复制任务）|核对残片并返回固定清理摘要|
|POST /api/organization/cleanup|id、digest、confirmed:true|持有任务/设备/全局互斥后，仅清理已证明归属的残片|
|POST /api/organization/prefer|id（video_id）、location|重新验证并切换播放位置；与物理整理互斥|

selected可以是预览中的index数组、all或eligible；后两者由服务端按这份固定预览展开。重复index、越界、空范围或摘要变化拒绝。eligible仅排除已记录冲突，仍须重新校验实际设备和源文件。

目标修改继续使用POST /api/storage/revise，生成新的盘点预览，再prepare和approve。不存在修改已批准清单的接口。反向移动不是永久删除。复制撤销使用专用unbind_copy草稿，复用undo-preview/undo-approve；普通approve不能批准这种草稿。执行仅解除本任务拥有的绑定，物理文件全部保留。默认目标须有现场验证通过且不在解除范围的替代位置；变化后须从原任务重新预览。见[ADR0004](../adr/0004-copy-binding-undo.md)。

## 状态与现场保护

任务waiting加paused:true表示用户暂停；其它waiting通常等待媒体资源。步骤分别记录暂存/复制、校验、提交、绑定，不能将文件存在当作已绑定。控制请求只改变意图，仍在运行的执行器自行到安全边界停止，不能从路由提前释放文件租约。

ResourceLeases提供跨流程读/写互斥；organization_leases补充设备互斥。未完成物理操作的organize-reconcile保护可以跨暂停和进程重启保留。只有对应任务/批准反向计划可以执行修复；普通修改不能借等待状态绕过保护。恢复备份前另核对文件操作历史，详见[数据规范](DATA.md)。

接口测试在test_organization_http.py使用临时资料库和真实HTTP覆盖，不能据此宣称全部设备、浏览器、迁移及性能验收完成。

## 硬盘查询与管理上下文

GET /api/context返回settings（来源凭据清空及has标记）、folders、collection_rules、excluded_directories、status、ffmpeg/ffprobe、platform/server_mode、amane和变更Token，transport=context-v1；不包含videos/movies/review，不构建片库全图。沿用Host与NAS鉴权，仅供已授权前端。旧/api/library保留，尚未完成主片库分页替换。

GET /api/storage/query参数：device=all或登记位置ID；online=all/online/offline；status=all/identified/unknown；quality=all/issue/no_known_issue；duplicate=all/historical；search；directory（根内相对目录）；sort=path/title/size/mtime；offset≥0；limit=1–100（默认50）；可选snapshot。响应items、total、bytes、offset/limit、criteria、snapshot/snapshot_created和devices别名/在线状态。每项仅当前页元数据白名单、companion_count和最多3项resource_preview；current_content_verified恒为false，历史指纹不伪装成现场校验。

第一次固定盘点范围并存有序标识；翻页必须沿用相同筛选和snapshot。设备/排除/建议/偏好变化、到期或淘汰返回409，条件无效400。新盘点不改变旧快照。有效30分钟、最多16份，不适合作为长期收藏地址或执行清单。数据库恢复后重建查询模块和触发器；查询缓存不是媒体或任务记录。


## 原位保留

POST /api/storage/plan新增operation=copy/move/keep（默认copy）；keep无需target，不创建目标/不计算复制空间，预览source=destination。GET organization/preview返回operation供恢复规则选择。organization/prepare支持keep，但仅接受原位预览；copy/move不可借用keep预览，storage/revise也不能把keep改成重命名。approve仍需confirmed:true和对应摘要。keep任务通过原队列运行，已有默认副本保持，只有源原位是当前播放路径时补齐其验证/可用状态。关联资源沿用身份校验，事件kind=kept_bound，未写物理媒体。取消后保留已完成关联；不提供虚假的文件反向移动入口。


## 复制后单独归档

POST /api/organization/archive-preview：id为已完成copy任务，返回独立草稿id；不移动源、不创建物理归档目录。GET draft返回operation=archive、archive_of、每项源/归档路径及archive_guard保留副本证据。POST archive-approve：id为原copy、draft、digest、confirmed:true；幂等排队返回新任务id。普通approve不能替代该确认。job返回archive_of、source_archives（最近20项）、以及已完成copy的archive_capability（supported/reason）；不支持时界面禁用并说明。

归档使用固定原设备namespace，不允许任意归档根输入。只支持策略确认的可写Windows NTFS/ReFS，实际本批NTFS测试。目标副本变化/外部同内容替换/绑定变化/源为默认等拒绝。控制沿用pause/cancel/resume，archive步骤按move持久化。undo-preview/undo-approve生成并批准恢复原路径的反向move；归档根继续排除。locations增加archived布尔，prefer拒绝归档位置；完整源与目标对账见ADR0005。


## 从固定查询选择到多来源计划

storage/query的每项增加query_ordinal。POST /api/storage/selection：snapshot、selected（ordinal数组/all/{exclude:[ordinal]}），返回total/bytes、各来源位置计数/在线状态、criteria、scope和query_total，不返回全量元数据。POST /api/storage/query-plan使用同样固定选择，加target、layout、operation=copy/move/keep；返回精简plan摘要（id/digest/total/bytes/source_roots/selection/space），具体条目通过原organization/preview分页读取。

过期、设备/排除/建议变化、成员不属于查询返回409；重复/类型错误/空规划400。选中离线项仍进入只读预览并列冲突，prepare拒绝执行；不偷偷缩成在线子集。新盘点不改变固定老成员。source_devices与逐项来源快照/路径属于计划摘要，prepare重新校验所选根/卷/完整内容。选择/规划/prepare请求上限2MB，其余保持原100KB。选中范围不是对后续新增文件的授权。

多来源仍返回一个执行草稿/任务ID；状态/控制/位置、归档/反向接口继续原契约。跨设备move逐项拒绝，使用copy保源。


## 范围调度（开发候选）

整理执行在同一事务登记文件/资产/设备范围和持久写入票号。等待的冲突写任务阻止新冲突读任务插队；已有读任务在文件边界结束后让出，无关文件播放/检查可继续。暂停/取消移除等待票号，不释放尚在执行的租约。队列会尝试被阻塞任务之后的不同设备任务，仍单执行线程及设备写入串行。部分预览/维护/同步接口保持全局，备份恢复全局独占；不是完整资源矩阵已验收的声明。


## 物理文件详情与片库定位（开发候选）

以下GET沿用Host/NAS鉴权，不接受任意路径读取，不批准或执行整理。

|路径|参数|响应与边界|
|---|---|---|
|/api/storage/file|snapshot、ordinal、resource_offset≥0|固定查询成员的原file_snapshot、设备别名/路径、历史大小/mtime/SHA、当前单文件stat、识别候选/锁/建议、当前资产/作品/版本/分段绑定；资源每页50|
|/api/storage/video|id（视频文件ID）、offset、limit≤50（默认20）|此文件的稳定资产和位置分页；尚未迁移的旧索引以indexed位置只读展示，不自动创建资产|
|/api/storage/locate|id、location|仅接受属于该视频的已有位置；有盘点则生成精确成员查询并返回snapshot/ordinal/criteria；无盘点返回indexed:true及video_id/location供索引详情使用|
|/api/storage/indexed-file|id、location、resource_offset|直接读取已有视频/位置记录与当前属性，明确source_type=index，无file_snapshot和盘点时间；不自动登记/盘点/入库，绑定资源每页50|

盘点详情source_type=inventory；名称/候选来自固定历史元数据，当前作品绑定单列，不将目录合集建议改写成版本关系。状态current只代表大小/修改时间匹配，不表示完整内容现场校验；current_content_verified恒false，SHA与校验时间明确为历史。offline、device_changed、changed、missing、permission_denied、linked_or_unsafe、unavailable分别显示；旧索引缺稳定卷记录则identity_unverified，不能当设备验证通过；旧索引沿用秒精度时间及10毫秒属性容差，不当作纳秒指纹证明。

查询到期/设备/排除/建议变化或indexed占位被实际位置替换返回409；未知ID、越界成员、外来位置及不安全相对路径400；资源冲突409。查看期间持路径/资产/视频范围读租约，不打开媒体内容、不触发刮削、不生成执行清单。位置未盘点也可读已有索引；若要完整盘点/整理，需要另行登记和确认。

前端地址storage-file保存m_snapshot/m_file或m_video/m_location，资源m_resource；storage-video使用m_source视频ID。文件列表原筛选/偏移及滚动/焦点保存在History状态，刷新详情可返回；查询过期时解释并回原筛选刷新，不扩大旧选择。物理位置与单作品版本/分段、目录合集分别展示。


## 统一预检与原目录移动

prepare/approve及copy/move执行边界都进行只读预检，不再依赖浏览器冲突标记。错误400含可读提示；已领取任务中的预检失败持久保存步骤details.preflight，job响应附preflight_messages。暂存前冲突保持源原位，暂存后继续按日志处理；不得清理移动暂存。

preview.items附conflict_messages；space含required（新增量）、data_bytes（视频+明确归属资源数据量）、reserve、free和operation。共享/不可用资源不计入复制。move新增量0、预留16MiB；copy按同目标设备合并所有选择与资源，保留100MiB/最大单文件/5%中的最大值。draft可附preflight摘要（不含浮动free），旧草稿仍会重检。

copy目标继续独立于全部所选源；move允许原盘点根或其子目录，修正大小写或对调只认本次选中的源。修改目标仍产生新计划/摘要。raw_target在规范化前拒绝链接/重解析点，普通路径可含合法隐藏目录。当前240 UTF-16单位限制同时包含随机暂存名；超过时缩短路径，不能仅看最终文件名长度。此规则不宣称已验证全部旧长路径恢复。

已有源完整证明不能由新检测静默替换；完整校验期间设备变化拒绝，不登记到新卷。所有检查是当时条件，执行时继续核对归属/指纹，不覆盖外部文件。详见ADR0008。


## 备份恢复文件对账（开发候选）

POST /api/restore-preview，正文name（已存在的主库备份名），要求Host/Token及全局恢复写租约。与恢复共用后台静止检查，不跳过任务。响应id、digest、backup、ready、verified_count、retained_count、problem_count，以及最多50个retained_files.path、problems（code/job/path）；完整私有清单、指纹与日志不返回HTTP。预览只读原媒体，并保存私有固定计划。

POST /api/restore沿用name/confirm精确名称，新增reconciliation:{id,digest}。有物理历史须指定已保存且重新全量审计一致的计划；任意布尔值无效。资料/文件/归档变化400，需重新预览；租约冲突409。ready:false不自动修复/清日志。兼容终止纯资料keep/unbind_copy时保留旧无计划入口。恢复前保留完整旧DB及私有回执，恢复不移动/删除视频；返回rollback备份名。前端未通过不显示确认按钮，完整核对后列出留盘副本及数量。

文件位置不符须先在原整理任务审阅/确认反向；绑定未完/暂存存在须从对应任务处理。备份中的物理位置本已匹配时无需撤销。对账不将只读复制输入算作已重新校验对象。私有计划和回执格式见DATA/ADR0009，不能作为公开附件。


## 停止任务后的文件组跳过（第三十二批）

POST /api/organization/skip-preview：id、ordinal（JSON整数，步骤序号）。只读，沿用Host/NAS/Token保护。返回id/ordinal/digest、items（ordinal/source/destination/role/state）、bytes。视频及asset_id关联的字幕等共同列入；不单独遗弃拥有者。任务必须停止，waiting须已暂停；queued/running/维护、已有撤销、archive/unbind_copy及不完整日志拒绝400。

POST /api/organization/skip：id、ordinal、digest、confirmed:true；确认不是布尔真或摘要/状态已变化均400。事务内核对并追加持久skip决定及事件，批准payload不变，不写媒体。copy只允许pending/copying/verified，move/keep只允许pending；任一组成员有暂存移动/提交/绑定/完成则拒绝。返回ok、skipped（本组条目数）、state。不自动启动其余步骤，仍需既有control/resume。

progress新增skipped计数，processed仍仅done；listing新增total/processed/skipped聚合，来自本页任务的步骤，不加载清单全文。全部done或skip且含skip时以cancelled结束，界面“已结束 · 含跳过”，绝不标全批done；此时resume返回400。其它错误仍failed/partial。再次处理跳过文件需新计划。

执行器claim后重读决定；清理复制残片保留skip。旧checkpoint继续供对账/清理使用；详细安全边界见[ADR0011](../adr/0011-explicit-precommit-group-skip.md)。不支持跳过撤销任务或已进入不可安全省略阶段的文件，不把拒绝当作故障已解决。
