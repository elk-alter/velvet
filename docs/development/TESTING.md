# 测试与验证规范

## 通用命令

```bash
python -m unittest discover -s . -p "test_*.py"
python scripts/release.py --check
git diff --check
```

release.py只检查Git跟踪/暂存文件；新增文件需先加入Git。它检查Python/JS语法、部分隐私模式、文档本地链接、静态资源与Docker、版本记录，不运行后端测试，也不替代人工秘密审查或浏览器验证。无需每次变更重复完整发布流程；按影响选检查，正式发布再完成全套。

## 测试边界

- 后端用临时SQLite、模拟服务和虚构路径，保持测试独立，不依赖个人账号/片库/在线站点。
- OCR规则测试不要求模型或网络；真实OCR验证另装可选依赖，用合成水印/字幕、记录模型版本及误报边界。
- FFmpeg集成用合成短片、截断副本和源文件SHA-256，禁止破坏原媒体。明确探测/抽样/全解码的范围。
- 任务覆盖重复提交、取消、子进程退出、恢复、重试上限、文件变化和人工覆盖。
- 数据变更覆盖空库、旧库、重复迁移、含队列备份恢复；不能只测全新数据库。

## 浏览器验收

相关场景选择验证：列表与空状态、筛选重置、分页/跳转、每页数量、前进后退、详情返回、分集/版本选择、批量范围、异步更新、未保存编辑、隐私遮图及跨标签页、排除/恢复、键盘与窄屏。

截图只用合成资料，保存在独立输出目录；不把私人封面、OCR文字或路径用作公开证据。页面能打开、脚本语法通过、后端测试通过均不代表交互通过。

## 报告格式

环境：系统/Python/浏览器/FFmpeg及相关依赖；命令/场景；通过/失败；验证数据类型；未覆盖项；复现步骤。测试数量是当次结果，新增测试后不要继续复制旧数字。

当前尚无强制CI、完整浏览器端到端、大库性能/真实NAS及全编码矩阵；这些是欠账，不在文档中假装门禁已启用。

## alpha.5工具

`python scripts/preview_alpha5.py`提供独立合成UI；`python scripts/benchmark_library.py --counts 10000 50000`量测接口；历史alpha.5验收使用从alpha.4迁移的脚本；该历史命令见ALPHA5-VALIDATION，当前alpha.6脚本见下节，不将旧命令冒充当前可复现入口。源码包不含.git，演练可从开发checkout执行；包内后端测试不依赖Git。不得将HTTP时间写成首屏时间，或把模拟FAT32写成实机验证。


## alpha.6实际跨卷合成演练

Windows可选命令：`python scripts/rehearse_cross_volume.py --target-parent D:/`。来源默认系统临时目录，也可显式--source-parent；两父目录必须已存在且位于不同实际卷。只在两者下各新建唯一临时子目录，写合成字节视频/字幕/哨兵和独立应用库；退出清理这些自有临时目录，不读或整理已有文件。不使用-O，演练依赖断言。

覆盖跨卷copy/完整哈希/提交检查点中断与恢复/绑定/复制解绑/同盘move及反向/选定人工资料与非任务文件保留，输出摘要不含路径/GUID。它不是物理拔盘、真正进程强杀、有效视频解码、exFAT或浏览器测试。仅选已有授权的测试位置，不用生产资料目录。


### alpha.6预检测试

test_organization_preflight使用独立临时文件，覆盖只读预检、大小写/Unicode、暂存名预算与UTF-16、原目录合法名称、所选资源整批容量、源证明及设备变化。Windows NTFS用可用pwsh/Windows PowerShell对自有临时目录/文件设置DACL，验证拒绝创建/读取后恢复原SDDL；输出不含账户SID或安全描述符。优先符号链接，当前机器无该权限时用自有目录junction验证拒绝，结束只移除自建链接。

FAT32/只读卷/空间容量为模拟；实际DACL不等于所有网络ACL通过。rehearse_cross_volume仍独立验证实际C/D NTFS，断点是合成SystemExit，不是物理拔盘。浏览器目标抢占使用自建外部哨兵，修正路径绕开，结束核对哨兵及源/目标哈希、mtime与受保护字段。


### alpha.6恢复与升级

`python -m unittest test_restore_reconciliation test_full_backup test_video_ids test_organization_http -q`覆盖主库/离线对账、完整回执、恢复ID水位、真实HTTP名称确认/鉴权/计划过期。移动六个检查点使用合成SystemExit，保留步骤并通过批准反向后对账；不是物理拔盘或进程强杀。

`python scripts/rehearse_upgrade.py`当前默认从alpha.5发布提交44f7373取源码，重复迁移后复制/移动/反向，恢复前完整对账、保留副本与回执，再启动旧程序核对18类资料、4张内置库表、两份配置及6个图片/OCR证据缓存。核对每一步人工决定，受控模拟执行队列且禁止外部请求，详见[升级验收](../ALPHA6-UPGRADE-VALIDATION.md)。可用--baseline指定兼容alpha.5模式的历史，不承诺任意旧版本；不是实际OCR识别或灾难恢复矩阵。源码包没有Git历史，演练从checkout运行，不能冒充包内测试。默认提交依据本地Git历史，不假设存在版本标签。

`python -m unittest test_upgrade_preservation -q`不依赖Git，覆盖已改变的人工资料/图像/内置缓存完整恢复与重启队列规则，随默认全量测试执行。scripts/upgrade_fixture.py仅用于合成演练，不导入生产数据。


`python -m unittest test_presentation_identity test_features test_library_query_core test_library_query test_server_browse test_location_core test_upgrade_preservation -q`核对身份/展示一致性、人工字段锁与查询/位置/恢复。历史键与聚合比对、同机HTTP计时的实际证据见ALPHA6-PERFORMANCE；没有浏览器绘制时不宣称首屏验收。


`python -m unittest test_movie_details test_movie_detail_view test_server_browse test_library_query test_library_query_core -q`覆盖分页/首选/续播/跨页下一段/隐藏成员编辑上限/NULL旧数据/schema1聚合迁移、鉴权与恢复/重启令牌、缓存适配与关闭后迟到响应。实际媒体解码和全部关闭/导航入口仍需独立验收。

`python scripts/benchmark_movie_details.py --counts 10000 50000 --output report.json`用独立进程覆盖单作品的极多分段和极多版本。每场景先记录索引准备，再测首次详情及20次暖HTTP、响应字节、每请求呈现文件数、进程峰值；禁止全作品hierarchy，超大编辑必须在加载前拒绝。HTTP不是浏览器绘制；较慢压力场景不靠删测或改目标放行。
