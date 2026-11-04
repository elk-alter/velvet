# 片库查询：alpha.6开发后端

尚未稳定的内部契约，主片库已使用分页与按需资料；部分旧管理消费者仍保留全量/api/library。接口接入不能单独作为A6-20–22整体验收证据；当前限制见[进度](../ALPHA6-PROGRESS.md)和[性能](../ALPHA6-PERFORMANCE.md)。沿用本机Host及NAS Basic鉴权；GET不绕过访问控制。

## GET /api/library/query

参数：q；mode=all/favorite/unwatched/recent/missing；length=all/short/long（1200秒分界）；censorship空/censored/uncensored/unknown/mixed；vr空/vr/flat；height=0/720/1080/1440/2160/4320；tags为最多50项JSON数组；tag_logic=all/any；exclude_tag；actor；studio；source空/matched/pending/error/partial；quality；sort=new/title/duration/rating/size；collections=on/off；collection为集合ID；offset≥0；limit=1–96（默认48）；可选snapshot。

quality支持no_cover以及needs_update（或update）、watermark/damaged/replace、suspected_watermark/suspected_black/suspected_freeze、decode_error、below_target/higher_local、auto_pending/auto_error/auto_unchecked、unchecked/active/inspection、pending/metadata_error/incomplete、accepted/ignored。实际封面集合变化会使缺封面查询及选择失效。质量命中与徽标看整作品各版本的文件，不因低质量替代版本而切换默认播放版本。

响应：items、total（归组后卡片数）、movies（命中作品数）、bytes、offset/limit、snapshot、criteria、revision。电影卡片kind=movie及稳定key、默认或命中版本的播放id/作品键/版本键、展示/技术/评分收藏进度、part_count/version_count和质量摘要；不含简介、完整版本/分段、锁或来源原JSON。合集卡片kind=collection及稳定id/key、title、hit_count/total_count、封面候选ID、size。集合筛选可保留其它条件查看命中，也可清条件看全集；独立页面已接，完整返回/并发边界仍待验收。

计数/字节与本次固定查询共用SQL范围，同值排序有稳定卡片键。新页必须传原筛选与snapshot；30分钟到期、超过16份淘汰、源/规则/偏好/关系/质量变化或构建期间上下文变化返回409，客户端重新查询并明确清理过期选择。条件无效400，认证/Host失败401/403。查询缓存不是批量操作授权。

## GET /api/library/movie?id=video_id

返回movie，内容与旧LibraryCore.hierarchy的单作品模型一致，含版本/分段、完整字段、人工锁、偏好与续播信息。按作品查询，不扫描呈现其它作品资料；不存在或排除400。此为保留的兼容响应，未附max_files时仍可能全量；新主详情使用下述detail分页接口。它不触发刮削或物理执行。

## 回归与限制

测试在test_library_query.py、test_library_query_core.py，覆盖SQL列表不构建全图、默认/替代版本、分段续播、归组命中/总数、排除/质量/人工关系和变更失效、按作品保真、重复初始化、并发规则变化及HTTP权限。所有样本隔离且合成。仍待首页客户端、全部筛选、选择、分页详情、大库冷首屏和完整发行验收。

## 第十一批前端接入及补充接口

主片库已切换到本契约；旧/api/library仍给未改造的管理消费者保留。以下新增接口同样沿用Host/NAS认证，POST还需Token。兼容movie及其它旧管理全量接口仍存在，不能据此宣称所有消费者都有大小上限。

- GET /api/library/query-revision?quality=...：源revision；仅quality=no_cover时附实际封面ID集合摘要。不要使用整个数据目录mtime（数据库日志会产生误报）。
- GET /api/library/facets?kind=tags/actors/studio&q=...：最多100项名称与作品数，供可输入筛选建议。
- GET /api/library/lookup?code=...：精确编号的可见本地ID，供未加载作品的任务重试，不靠当前页搜索。
- GET /api/library/relationship-options?q=...：最多100作品/100版本候选，供按需关系编辑；名称歧义不能静默选第一项。
- POST /api/library/selection：snapshot、keys（1–500个唯一卡片键）、offset、limit≤100；返回固定成员分页、total（作品）、files（命中版本文件）、bytes及eligible（有编号的作品）。越界卡片400，过期/源变化409。合集只展开冻结查询中的命中作品。它只解析范围，不执行文件或刮削。
- GET /api/library/random?snapshot=...：从有效查询命中作品随机返回一个播放ID。

query新增facet_kind/facet_name，按实际作品成员判定分类命中；quality支持no_cover，实际封面集合变化使该旧查询及选择失效。响应增加summary（默认版本的一致作品/收藏/字节/未看汇总）、collection_info。同revision/criteria的有效范围可复用；scope缓存只存卡片身份，页内连接当前一致读模型补资料；browse_query_members保存固定归属和命中版本。仍为最多16份/30分钟。

movie响应同时附review，仅查询该作品的文件检查记录，完整证据仍在原质量接口。前端只缓存20个已打开作品，身份合并/拆分会清理交叠作品及孤立质量记录；关系编辑最多100文件，独立加载选择，避免被该缓存上限截断。选择在资料/筛选变化后清空；请求加载期间禁选，离开页面取消旧请求并隔离迟到响应。

批量刮削先捕获snapshot/keys，不随后续筛选改动；每批50个有编号ID沿用原提交接口，跳过无编号，范围失效则停止后续并保留已提交反馈。模拟150任务通过，不代表真实来源刮削成功；当前任务工作台仍显示最近100条。

第二十三批只调整内部身份派生与版本聚合中间列，GET响应字段、固定选择/过期、偏好/质量语义不变。最新五万首次HTTP6.304秒仍不达冷首屏目标；该批单作品版本/分段分页尚未接入；后续第二十四批的契约见下。


## GET /api/library/detail（第二十四批开发候选）

主片库详情已使用此接口，不调用全作品hierarchy。参数id为可见视频文件ID；exact=0（默认）遵循作品首选/续播，exact=1打开该文件。version_offset≥0、version_limit默认10/上限20；part_offset≥0（省略时自动定位所选分段所在页）、part_limit默认20/上限50。后续页带原样revision，不将令牌解析为数字。

响应detail包含movie_key、selected完整当前文件、active_version/default_version摘要、default_version_key、total_files（可见文件）、edit_file_count（全部索引成员）、favorite/mtime、revision，以及versions/parts的items/offset/limit/total。版本摘要无完整分段图；当前和默认版本可固定列在当前页以外。parts按人工part_no与文件ID排序，selected不在正在浏览页时仍单独返回；单次最多呈现50个页内文件加1个当前文件。外层review只覆盖这些文件，跨版本质量依据仍取完整SQL范围。

next是当前版本中紧邻的下一可见分段{id,missing}或null，不依赖当前页，不自动跨过缺失分段。每个版本有可区分的目录/文件提示及最低画质说明；摘要和原始物理文件身份分开。读取不探测原视频、刮削或批准整理。

令牌绑定读取实例和源revision；资料变化、服务重启或HTTP恢复后旧页409，前端保留编辑并提供重新打开。未知/已排除文件、非法参数、越界页400；Host/NAS鉴权沿用403/401，不接受任意路径读取。

旧GET /api/library/movie可附max_files=1–100供关系编辑：后端先按全部索引成员计数，在应用数据库互斥范围内检查后才加载完整图；超过限制400。隐藏成员也计入上限。主详情大作品只编辑当前文件且明确提示；跨页批选过大仍拒绝，不偷偷裁成当前页。旧无参数兼容接口保留，不给新主详情使用。

前端20作品缓存现在可含分页分段；当前文件始终保留。关系编辑显式取得完整小范围或当前ID，不能从部分缓存推导“全部”。详情页取消、关闭/切换与迟到响应隔离；翻页不重建正文，版本页码与键盘焦点保留。默认版本操作使用原API并刷新导航。
