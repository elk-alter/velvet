# 模块职责与接口约束

|模块|职责|扩展要求|
|---|---|---|
|app.py|HTTP、主库初始化、扫描与编排|新增业务优先抽模块，路由仅校验/调度，不复制状态|
|library_core.py|影片/版本/分段、偏好、备份关系|目录合集展示不能改写版本关系|
|media_libraries.py|媒体库政策、路径归属、目录排除|统一路径判断，区分通配符与精确目录|
|media_naming.py|通用名称与父目录回退|固定样本，尊重人工/NFO/已匹配标题|
|builtin_scraper.py、scrapers/|采集、独立队列、来源缓存及适配|保留来源/许可证，编号验证、失败分类、凭据隔离|
|network_checks.py、source_catalog.py|诊断与来源能力清单|连通不等于匹配成功，清单不等于已移植|
|people_catalog.py、artwork_store.py|人物/分类、图片任务|失败不覆盖有效缓存，资料与图片分开结算|
|file_review.py、visual_analysis.py|只读检查、自动OCR、证据与人工审阅|隔离子进程、取消、指纹失效；自动与人工分开|
|index.html、browse.js/css|浏览、分页、合集和基础交互|现有全局状态耦合，避免新增多层包装|
|management.js、manager_routes.js|管理渲染、路由/请求生命周期|地址、返回、脏表单和轮询一致|
|film_detail.js、file_review.js|详情/画廊、质量、隐私等|当前文件与影片范围明确；后续逐步拆分职责|
|scripts/release.py|公开源码检查和归档|不得导入运行服务访问生产资料|

## HTTP接口

目前未提供稳定版本化API，不应宣称向后兼容承诺。修改接口需记录方法、路径、字段、默认值、校验、返回、错误、鉴权、状态变化和消费者；同一提交同步前端与测试。

沿用现有本机Host检查、NAS认证、变更请求Token和大小限制，不因新增路由绕过保护。路径由服务端受控解析，文件接口使用已有记录ID与白名单；不接受任意文件读取。失败返回需可操作且避免凭据泄露。

新静态资源同步app.py的UI_ASSETS、页面引用、Dockerfile及资源回归。只改本地开发可用但归档/Docker缺文件不算完成。

目标架构是清晰的路由/服务/仓储/任务/视图边界；这是重构方向，当前尚未完全实现。不要把文档中的目标结构误写成现状。

## alpha.5模块

- runtime_config：端口、监听与诊断白名单；service_control：本机后台启停及身份核验。
- data_guard：数据目录跨进程所有权；full_backup：离线完整备份/恢复。
- task_journal：任务逻辑ID、事件及既有执行器状态对账。
- directory_policy：展示规则与预览摘要；media_libraries：共同可见性判断。
- storage_inventory：只读设备/快照、建议修正、比较和不可执行计划；不承接源文件操作。
- library_transport：重复文件元数据的无损引用传输；原全量接口保留。

数据取舍见[ADR0001](../adr/0001-alpha5-reliability-and-readonly-plans.md)，性能限制见[测量报告](../ALPHA5-PERFORMANCE.md)。

## alpha.6开发模块（开发候选，尚未整版验收）

- resource_leases：跨流程持久范围读/独占写租约及冲突写者FIFO；播放、按根扫描、逐文件检查/图片、刮削缓存已分范围。部分同步/重复检查/通用HTTP/预览/维护仍全局，备份恢复全局独占；不能称全部细粒度完成。
- media_locations：稳定文件身份与经过验证的位置绑定、多位置扫描去重和待入库身份复用。
- media_companions、companion_bindings：伴随资源归属、名称映射及事务绑定，不把资源建成视频。
- disk_manager.js：独立硬盘工作区、固定计划确认、执行/恢复与分页选择；manager_routes持有地址和生命周期。
- organization_service：盘点预览到身份注册/执行批准/后台调度/绑定的应用服务，支持未入库原文件；HTTP/UI及明确归属的伴随资源已接；完整范围与缺口见进度。
- organization_jobs：不可变批准、执行步骤、互斥租约、持久取消与启动对账。
- organization_executor：已批准copy计划的暂存、完整校验、不覆盖提交与绑定回调。
- organization_cleanup：已停止copy任务的残片清理预览、日志及Windows句柄安全删除；不清理移动暂存或已提交目标。
- organization_moves：已批准同盘move计划的全批暂存、循环改名、绑定及重新批准的反向任务；伴随资源经服务适配，完整恢复和设备验收仍未完成。

当前证据与缺口见[alpha.6进度](../ALPHA6-PROGRESS.md)。接生产初始化时须同步Docker资源、关系稳定策略及跨执行器租约。

- inventory_query：跨盘SQL范围、重叠根去重、固定排序标识缓存及分页DTO；独立于整理批准和主片库全图。

- library_query：开发中的片库派生投影、SQL版本/合集聚合、固定分页卡片与按作品详情后端；主片库前端已接server_browse。FileReview.describe为旧/新查询共享的单文件质量推导。

- server_browse.js：分页请求/取消、固定选择、合集URL/返回、按作品资料缓存、范围刮削及有限筛选/关系建议；index/browse的交互处理器显式调用，替代主页面全量bootstrap。

- video_ids：数字视频ID的原子旧表迁移、缓存/本地历史引用水位及备份恢复预迁移；只读应用缓存，不改原媒体。见[ADR0003](../adr/0003-monotonic-video-ids.md)。library_query的分页前端已由server_browse接入。

- organization_copy_undo：复制绑定撤销的固定审阅/专用批准/原子元数据事务，与既有队列、控制、租约和反向任务关系共用；不写物理媒体。

- organization_keep：原位保留执行器；复用固定批准、控制、租约及稳定身份/资源适配，校验和关联现有文件，不写媒体目录。

- organization_archive：已完成复制后的独立归档预览/批准、目标副本证明、设备能力和持久来源关系；复用MoveExecutor执行/恢复。archive_policy集中保留namespace和强制根排除，供片库、盘点、查询、播放和副本解绑使用。

- organization_planner：从InventoryQuery固定成员构建多来源计划，复用原模板和冲突预检；不把查询范围当执行授权。disk_selection.js独立管理物理跨页/全部匹配差集和返回参数，disk_manager显式接入。


- storage_file_details：固定盘点成员详情、已有索引/稳定位置详情和片库定位；只读stat与身份检查，历史证据/当前属性/作品关系分开。通过已有记录ID限定范围，无资产自动迁移/自动入库或文件执行。
- disk_file_view.js：物理文件及位置视图、索引回退、资源分页与返回上下文；manager_routes持有地址、History滚动/焦点，film_detail提供入口。新资源与UI_ASSETS/HTML/Docker同步。


- organization_preflight：只读名称/链接/权限/容量预检、合法名称验证和人类可读代码映射；草稿、批准、执行及提交边界接入。no_links在此实现，organization_jobs保留兼容导出；safe_component仍只负责生成名称。
- storage_inventory/organization_planner：共用预检名称索引与按操作估算；copy保留独立目标，move可在原根内整理。organization_service回传预检摘要/提示并再次核对盘点设备，不自动采纳替换卷。


- restore_reconciliation：只读重放物理日志、完整文件证明、权威DB/归档/配置摘要、固定计划与私有回执；不执行文件修复。restore_safety验证内部新审计许可，兼容已终止纯资料操作。
- library_core：主库恢复预览和最终门禁/回滚备份；app负责全局独占、后台静止及有限HTTP结果。full_backup复用审计，持DataGuard离线恢复并保留旧目录/私有回执。index/manager_routes显示问题和留盘副本、传计划令牌，不自己推断安全。详见ADR0009。

- scripts/upgrade_fixture.py：alpha.5兼容合成资料、图片/OCR证据和队列样本，供Git历史升级演练及无Git的回归测试共用；不进入运行时，不处理生产数据，不修改已有源媒体。

- media_presentation：从app抽出的只读展示与轻量identity_presentation，共用分段/VR/内容标记规则；app保留原名称导出。LibraryCore可注入identity_traits(row,path)，缺省沿用自定义presentation兼容旧调用者；恢复重建保留该回调。它不分配媒体UUID或改写人工关系。
- projection_presentation(row,path)只返回列表投影所需的标题/VR/内容类型/分辨率，display_title与完整展示共用人工锁/NFO/来源保护及目录回退；不生成详情part_key等字段。LibraryQuery可注入project(row,raw_path)，缺省继续调用core.present，旧构造和自定义展示兼容。app初始及恢复重建均显式传入窄投影；raw_path保持原拼写，规范化路径仅用于权限/可见性，不反向改变命名依据。
- library_query版本聚合的files中间集只带统计/排序必需列，代表版本再联结完整展示；持久读模型字段与查询/失效契约保持。
- 匹配范围的ranked/browse_matches只保留身份、集合、排序/数量所需列，页面展示再按movie/version键读取；固定选择、默认/命中版本与总数不从当前页推断。查询契约及schema3不变，不能省略规则/排除/质量过滤以换速度。

- movie_details：单作品/版本/分段分页、首选/续播、相邻分段与完整质量依据；无全图呈现，读取实例在HTTP恢复后换代。for_edit对全部索引成员先限制100，再兼容完整小图，保持应用DB互斥范围。
- library_query的schema3版本聚合预计算最大可见mtime及该时间最小ID供详情排序；仅派生投影升级，业务字段/关系键不变。schema1/2复用逐文件投影，不能为性能隐藏预热成本。
- movie_detail_view.js：详情页导航、固定当前/默认项、未保存切换保护、页码/焦点/取消及迟到隔离；server_browse只缓存有界页面，关系编辑用明确范围，不能从部分缓存推断全片。UI_ASSETS/HTML/Docker同步。

- file_review的当前质量编辑器保存单文件note/decision基线，重绘先保留尚未保存的值、成功保存后与权威结果比较；close或确认移除库记录后释放草稿。已有openVideo包装转发上游结果，false不重绘，也不吞掉自动续播取消信号。index的detailHasChanges聚合影片资料与质量表单状态，server_browse/disk_file_view/manager_routes复用，不另叠导航包装。

- file_review的requestDirectoryExclusion持有专用确认dialog，确认内容含影响范围与丢弃草稿说明；应用期间保持modal并禁止取消/重复提交。confirmDirectoryExclusion按原详情ID/代数审计生命周期，失败或陈旧预览不清理，新详情不受迟到结果影响；仍使用既有后端摘要校验。
- index播放启动先配置元数据回调再主动play，使用当前播放请求代数；ended在进度和refresh前后核对原详情/文件/DOM源/请求代数/ended状态，分页下一段来自后端next。它不是文件身份或持久任务代数，不新增全局导航包装。
- scripts/preview_alpha6_playback.py仅开发合成验收，生成VP8 WebM和哨兵，独立数据/端口、无扫描/来源线程；退出校验媒体SHA及mtime。不进入运行时和Docker资产表。


- library_query第三十一批：无file_reviews记录时，当前refresh事务的两种目标画质摘要复用权威FileReview.describe结果；非空记录逐项处理，不缓存其人工/自动证据。固定匹配先生成当前连接TEMP browse_cards，统计与排序复用一次聚合；原持久范围、上限、接口及schema3保持。脚本preview_alpha6_browse为不预建关系/投影的独立浏览器性能样本，不是运行初始化入口。
