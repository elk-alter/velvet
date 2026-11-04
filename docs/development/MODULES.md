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

## alpha.6开发模块（未接运行入口）

- media_locations：稳定文件身份与经过验证的位置绑定。
- organization_jobs：不可变批准、执行步骤、互斥租约、持久取消与启动对账。
- organization_executor：已批准copy计划的暂存、完整校验、不覆盖提交与绑定回调。
- organization_cleanup：已停止copy任务的残片清理预览、日志及Windows句柄安全删除；不清理移动暂存或已提交目标。
- organization_moves：已批准同盘move计划的全批暂存、循环改名、绑定及重新批准的反向任务；伴随资源和HTTP/UI尚未接入。

当前证据与缺口见[alpha.6进度](../ALPHA6-PROGRESS.md)。接生产初始化时须同步Docker资源、关系稳定策略及跨执行器租约。
