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
|POST /api/organization/prepare|id（盘点预览）、digest、selected、operation|完整复检并建立不可变执行草稿；operation为copy或move|
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
