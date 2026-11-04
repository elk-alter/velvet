# 片库查询：alpha.6开发后端

尚未稳定的内部契约，旧片库前端仍使用全量/api/library。新增后端不能作为A6-20–22交付或首屏通过证据；当前限制见[进度](../ALPHA6-PROGRESS.md)和[性能](../ALPHA6-PERFORMANCE.md)。沿用本机Host及NAS Basic鉴权；GET不绕过访问控制。

## GET /api/library/query

参数：q；mode=all/favorite/unwatched/recent/missing；length=all/short/long（1200秒分界）；censorship空/censored/uncensored/unknown/mixed；vr空/vr/flat；height=0/720/1080/1440/2160/4320；tags为最多50项JSON数组；tag_logic=all/any；exclude_tag；actor；studio；source空/matched/pending/error/partial；quality；sort=new/title/duration/rating/size；collections=on/off；collection为集合ID；offset≥0；limit=1–96（默认48）；可选snapshot。

quality当前支持needs_update（或update）、watermark/damaged/replace、suspected_watermark/suspected_black/suspected_freeze、decode_error、below_target/higher_local、auto_pending/auto_error/auto_unchecked、unchecked/active/inspection、pending/metadata_error/incomplete、accepted/ignored。尚未包含旧页面缺缓存封面等全部筛选，前端替换前须补全。质量命中与徽标看整作品各版本的文件，不因低质量替代版本而切换默认播放版本。

响应：items、total（归组后卡片数）、movies（命中作品数）、bytes、offset/limit、snapshot、criteria、revision。电影卡片kind=movie及稳定key、默认或命中版本的播放id/作品键/版本键、展示/技术/评分收藏进度、part_count/version_count和质量摘要；不含简介、完整版本/分段、锁或来源原JSON。合集卡片kind=collection及稳定id/key、title、hit_count/total_count、封面候选ID、size。集合筛选可保留其它条件查看命中，也可清条件看全集；独立页面尚未接。

计数/字节与本次固定查询共用SQL范围，同值排序有稳定卡片键。新页必须传原筛选与snapshot；30分钟到期、超过16份淘汰、源/规则/偏好/关系/质量变化或构建期间上下文变化返回409，客户端重新查询并明确清理过期选择。条件无效400，认证/Host失败401/403。查询缓存不是批量操作授权。

## GET /api/library/movie?id=video_id

返回movie，内容与旧LibraryCore.hierarchy的单作品模型一致，含版本/分段、完整字段、人工锁、偏好与续播信息。按作品查询，不扫描呈现其它作品资料；不存在或排除400。当前单作品极多分段仍未分页，后续需版本/分段独立分页契约。它不触发刮削或物理执行。

## 回归与限制

测试在test_library_query.py、test_library_query_core.py，覆盖SQL列表不构建全图、默认/替代版本、分段续播、归组命中/总数、排除/质量/人工关系和变更失效、按作品保真、重复初始化、并发规则变化及HTTP权限。所有样本隔离且合成。仍待首页客户端、全部筛选、选择、分页详情、大库冷首屏和完整发行验收。
