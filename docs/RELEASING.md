# 发布流程

适用：0.1.0-alpha.2 起。源码发布不等于 Windows 安装包或容器镜像发布。

## 发布候选门槛

以 [规划索引](PLAN-INDEX.md) 指向的版本需求和 [交付台账](DELIVERY-STATUS.md) 核对 P0 验收。候选记录应包含提交、测试环境、通过项、剩余问题、设备覆盖和升级回滚证据。范围变更按 [开发流程](development/WORKFLOW.md) 记录。候选提交一旦变化，复核受影响证据并重新生成附件和校验和。发布后核对标签、附件和版本说明。部署另记运行版本及健康结果。

## 准备

1. 核实 [功能清单](FEATURES.md)、[需求表](REQUIREMENTS.md)、[路线图](ROADMAP.md) 与实现一致。
2. 更新 VERSION、README、CHANGELOG 和对应版本说明；保留历史版本文档。
3. 使用 Python 3.10+ 和 Node.js；安装 `requirements.txt`，准备 Git。Node.js 只用于开发/发布语法检查，不是应用运行依赖。
4. 运行 `python -m unittest discover -s . -p "test_*.py"`；界面变更补浏览器验证。
5. 将预期公开文件加入 Git 暂存区，再执行 `python scripts/release.py --check`。检查只覆盖 Git 已跟踪/暂存文件，不能代替人工隐私审查。

检查内容：公开路径、常见凭据模式、文档本地链接、Python/外部及内联 JS 语法、前端静态路由与 Docker 资源、版本与变更记录。检查不访问个人片库，不导入 app.py，不请求资料来源。

## 构建与发布

```bash
# 检查通过后提交，工作树必须干净
python scripts/release.py --output dist
# 校验/解压源码包，在隔离目录运行同一套测试
# 本次版本示例：
gh release create v0.1.0-alpha.2 dist/velvet-0.1.0-alpha.2-source.zip dist/SHA256SUMS.txt --prerelease --title "Velvet 0.1.0-alpha.2" --notes-file docs/RELEASE-0.1.0-alpha.2.md
```

源码包来自已提交的 `git archive HEAD`，只包含版本化文件。核对发布标签指向测试过的提交；发布时建议附 `--target` 指定该提交。SHA256SUMS.txt 是完整性校验，不是数字签名。GitHub 自动源码归档可能与附件 ZIP 字节不同，校验和只针对附件。

源码 ZIP 不含 Python、FFmpeg、依赖环境、原视频、私人图片、数据库和 Token。不能保证仅靠模式检查发现所有秘密，仍须审查新增文件和二进制附件。CI 示例尚未激活。

## 升级及回滚

按 [部署与升级](DEPLOYMENT.md) 停止任务并保存完整数据目录。源码应解压到新目录，保持旧程序可回滚，显式将 VELVET_DATA 指向新版本的数据副本。不要覆盖或上传运行数据。升级后检查健康版本、媒体库数量、任务、图片和偏好；数据库迁移不应假定能被旧程序反向兼容，回滚应恢复升级前的数据快照。
