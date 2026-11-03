# alpha.5 运行、备份与硬盘盘点

## 启停

Windows双击根目录“启动.bat”会后台启动，HTTP健康核验后打开浏览器。关闭启动窗口不会停止服务；已有同一数据实例时复用。开发调试仍可用python app.py在前台运行。

```powershell
python service_control.py start --open
python service_control.py status
python service_control.py restart --timeout 60
python service_control.py stop --timeout 60
# 独立数据与端口
python service_control.py start --data D:/Velvet-Test --port 18765 --open
```

端口也可由VELVET_PORT设置；重启不指定端口时沿用已验证实例的端口。控制器当前只支持本机模式；NAS使用容器管理器停止/启动。端口冲突不结束占用进程。启动日志在选定数据目录的service.stdout.log/service.stderr.log；它们可能含私有信息，不上传公开Issue。

安全停止不改写自动化设置。超时会恢复服务领取并报错，请检查任务中心后再重试；不会静默强杀。任意端口的第二个实例都不能共用已占用的数据目录。

## 完整备份与恢复

先用控制器停止服务；对旧alpha.4应按原版停机流程确认完全退出。数据目录及其父目录需可写，备份文件放在独立位置。

```powershell
python full_backup.py create --data D:/VelvetData --archive E:/Backups/velvet-before.zip --include-cache
python full_backup.py restore --data D:/VelvetData --archive E:/Backups/velvet-before.zip
python service_control.py start --data D:/VelvetData --open
```

完整备份包含library.db、存在的builtin-scraper.db、根目录配置JSON；可选缓存包括头像、封面、截图和OCR证据图片。不包含原视频、浏览器localStorage偏好、服务日志、旧备份或运行控制Token。配置可能含来源凭据，备份须保密。

恢复先校验路径、重复条目、清单、大小、SHA-256及SQLite，再替换目录；原数据保留为同级before目录。错误不会替换现库。归档容量上限100GB，超出需另行设计备份方案。旧版回滚应使用旧程序加升级前完整备份。网页的“备份恢复”仍是主片库备份，范围较窄，不能代替完整备份。

## 任务与目录政策

“刮削中心 → 全部后台任务”汇总近期任务，可查看阶段、数量、排队与等待原因，并导出脱敏诊断。重试和取消仍在对应任务入口：暂停自动化只停止新自动领取；扫描/盘点取消保留已完成项目；同步/图片在安全边界停止；文件检查取消包含排队项目；元数据任务按远程ID取消，提交结果未知须人工核对。手工重新刮削绕过缓存，自动刮削采用缓存及每日、最多三次的失败重试政策。

合集规则通过“合集规则 → 预览并应用”设置；最深人工规则优先。它只改变当前筛选结果的展示，不改变影片、版本、分段、收藏或播放记录。排除前列出受影响文件及影片数（受影响不等于整部影片都被隐藏）；恢复立即显示已有索引，新文件需要扫描。

## 硬盘盘点

在“媒体库 → 硬盘盘点”登记范围与别名。盘点只读；完整哈希会读取全部视频字节，耗时与磁盘速度有关。源文件在盘点期间变化或读取失败会记录错误。离线设备保留历史快照，不能把历史哈希当作当前验证。

快照可筛选未知作品或质量问题，修正作品/分集/版本建议；这些仅用于整理预览，不覆盖片库关系或人工元数据。跨盘比较支持历史SHA-256相同与同编号版本候选，只记录保留偏好，不删除副本。

选择独立目标目录后，可预览作品、保留原目录、版本、分集四种模板。共享资源和不确定归属必须人工复核；非法路径、大小写/Unicode冲突、已存在目标、长路径、空间及文件系统限制会提示。选择小批量及修改目标会生成新方案，原计划保留。JSON导出包含私有路径，请保存在本地。

此版没有复制、移动、重命名或删除执行入口；源变化后可修订重新核对，不能凭旧方案执行文件操作。只实测Windows NTFS；其他文件系统及NAS仍需验证。
