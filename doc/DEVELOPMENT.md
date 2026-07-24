# 开发与实现说明

## 项目结构

```text
astrbot_plugin_jmdownloader/
├─ main.py                 # AstrBot 命令和事件入口
├─ jm_service.py           # 下载、限制、缓存、PDF 和发送逻辑
├─ runtime_store.py        # SQLite 用量、事件、域名状态和指标
├─ config_tools.py         # 配置导入、导出与严格验证
├─ _conf_schema.json       # AstrBot 配置页面定义
├─ metadata.yaml           # 插件元数据
├─ requirements.txt        # 运行依赖
├─ requirements-dev.txt    # 开发依赖
├─ scripts/package.ps1     # 7-Zip 打包脚本
├─ tests/                  # 自动化测试
├─ pages/dashboard/        # AstrBot 原生管理 Page
└─ doc/                    # 扩展文档
```

插件运行时使用 `requirements.txt` 安装的 `jmcomic` 包，不依赖仓库中额外拉取的 JMComic-Crawler-Python 源码目录。

## 处理流程

```text
命令输入
  → 规范化 JM 号
  → 检查密码、用户速率、每日配额、冷却、重复任务和队列
  → 验证缓存或读取章节
  → 必要时等待章节选择
  → 按章节及页数限制下载
  → 使用精确图片清单生成 PDF
  → 生成并验证 AES-256 ZIP
  → 写入缓存清单并发送文件
```

耗时的站点访问、图片下载、PDF 合并和压缩工作通过后台线程执行，避免阻塞 AstrBot 消息事件循环。

## 章节与页数限制

页数限制必须在下载阶段生效，而不能只在 PDF 阶段截断。插件会先检查章节详情，再截断当前章节的 `page_arr`，只调度允许范围内的图片。

PDF 生成时会再次统计精确图片路径，并拒绝以下情况：

- 输入图片数量超过配置上限。
- 不同页面解析为相同文件路径。
- 图片不存在或为空。
- 最终没有可用图片。

目录规则会自动补充 `Pid`，避免多个章节共用图片目录。PDF 由插件直接调用 `img2pdf` 生成，上游 `after_album/img2pdf` 配置会被移除，以免完整漫画被再次扫描和重复打包。

## PDF 体积与重复页面

`img2pdf` 对标准 JPEG 通常采用直接嵌入，PDF 体积应接近所有图片的总大小。旧目录规则让多个章节共用目录时，同一批图片可能被每章重复读取，表现为 PDF 页数和体积成倍增加。

当前实现通过章节独立目录、精确图片清单、重复路径检查和 PDF 页数清单共同避免这一问题。

## 缓存与加密

缓存命中需要同时满足：

1. ZIP 和 JSON 清单存在且有效。
2. 文件名、JM 号、章节选择和缓存格式版本一致。
3. 章节及页数限制与当前配置一致。
4. ZIP 仅包含一个无目录路径的 PDF。
5. ZIP 条目已加密，并能使用当前密码解密。
6. 解密内容以 `%PDF-` 文件头开始。

压缩包使用 `pyzipper` 创建 AES-256 ZIP。缓存清单不会保存密码或密码散列；密码变更通过实际解密验证发现。

生成过程先写入临时 ZIP 和清单，验证通过后再替换正式缓存，防止未完成文件被发送。

## 持久化、配额与指标

`runtime_store.py` 使用 Python 标准库 SQLite，并启用 WAL。每次操作使用独立连接和进程内锁，重要计费通过 `BEGIN IMMEDIATE` 保证检查与写入原子性。

数据库包含：

- 速率窗口请求记录。
- 按本地日期和用户 ID 汇总的下载次数与发送字节数。
- 结构化任务、发送、失败、取消、域名和缓存清理事件。
- 域名健康状态、延迟、HTTP 状态与脱敏错误摘要。

每日次数与流量在调用平台文件发送前预留；发送失败后回滚。WebUI 只展示结构化字段，不读取 AstrBot 原始日志。

## 域名自动切换

后台任务使用当前 JM 客户端实现、代理、Cookie 和浏览器指纹逐个请求候选域名。排序策略为：健康域名按延迟排列、未知或过期状态保持配置顺序、失败域名最后。

JMComic 的 HTML 客户端可能在请求成功重定向到 `/error/album_missing` 后才抛出 `MissingAlbumPhotoException`，也可能在 Cookie 失效或站点返回异常内容时抛出 `RegularNotMatchException`。插件因此对章节检查和正式下载分别建立单域名客户端；遇到语义缺失、请求重试耗尽或响应解析失败后，清理本次任务目录并尝试下一个域名。日志记录候选域名、真实响应域名、路径、状态码、重定向次数和脱敏失败代码，不记录 Cookie。

用户可见提示和结构化失败原因使用统一诊断分类，包括疑似 Cookie 失效、需要登录或访问受限、本子不存在或访问受限、JM 网络请求失败、站点响应解析失败、文件系统读写失败以及 PDF/压缩包生成失败。底层异常摘要只进入脱敏日志和事件详情。

域名发现调用 JMComic 的永久入口、发布页和 GitHub 回退能力，与管理员当前配置合并后执行健康检查。一次性登录使用独立的空 Cookie 客户端，成功后仅合并返回的 Cookie；用户名和密码不进入配置、返回体或结构化日志。

## PDF 图片压缩

启用压缩时，Pillow 会执行 EXIF 方向修正、可选等比例缩放、透明背景白底合成和 JPEG 重编码。压缩图片写入任务临时目录，原始下载图片不修改。随后仍使用精确路径列表调用 `img2pdf`。

压缩开关、JPEG 质量和最大宽度写入缓存清单。仅在启用压缩时，质量或宽度变化会使缓存失效。

## AstrBot 管理 Page

`pages/dashboard/` 使用原生 HTML、CSS 和 JavaScript，通过 `window.AstrBotPluginPage` bridge 调用插件 Web API。后端路由由 `main.py` 注册，提供概览、事件、任务取消、域名检测与发现、一次性登录、缓存清理及配置工具。

Page 运行在 AstrBot 受限 iframe 中，不访问父页面 DOM、Dashboard Cookie 或 LocalStorage。配置导入由后端重新按 `_conf_schema.json` 验证，前端验证结果不能作为信任边界。

## 安全边界

- JM 号和章节选择有格式及长度限制。
- 用户可见错误和日志摘要会隐藏 Cookie、密码、令牌和代理认证信息。
- 一次性登录限制请求体和字段长度，使用后立即丢弃账号密码，只持久化 Cookie。
- 全局队列、单会话任务数和下载并发均可限制。
- 相同任务会合并，过期章节选择与发送冷却记录会自动清理。
- 高级 `plugins` 配置可能加载上游插件，只应接受管理员配置的可信内容。

## 本地开发

安装运行和开发依赖：

```powershell
python -m pip install -r requirements.txt -r requirements-dev.txt
```

执行检查：

```powershell
python -m ruff check main.py jm_service.py runtime_store.py config_tools.py tests
python -m pytest
python -m py_compile main.py jm_service.py runtime_store.py config_tools.py
node --check pages/dashboard/app.js
```

GitHub Actions 会在 Windows 和 Python 3.12 环境执行静态检查、测试、编译及真实 7-Zip 打包。

## 打包

在项目根目录执行：

```powershell
pwsh -File .\scripts\package.ps1
```

默认生成带版本号的安装包，例如：

```text
dist\astrbot_plugin_jmdownloader-v1.2.0.zip
```

指定格式或输出路径：

```powershell
pwsh -File .\scripts\package.ps1 -ArchiveType 7z
pwsh -File .\scripts\package.ps1 -OutputPath .\dist\custom-name.zip
```

脚本通过 Git 和 `.gitignore` 生成文件清单，排除测试、CI 和开发配置，并检查元数据、配置 Schema、必需文件和示例配置中的疑似凭据。旧输出会在打包前删除，避免残留历史文件。

## 发布前检查

1. 更新 `metadata.yaml`、README、Page 和 CHANGELOG 中的版本号。
2. 确认示例配置不包含真实 Cookie、密码或代理凭据。
3. 执行完整测试、静态检查和编译检查。
4. 运行打包脚本并使用 7-Zip 测试压缩包完整性。
5. 确认压缩包根目录包含 `metadata.yaml`，且不包含测试、缓存和本地配置。
6. 创建与 `metadata.yaml` 版本一致的 Git tag，例如 `git tag v1.2.0`。
7. 使用 `git push origin v1.2.0` 推送标签；Release 工作流会重新检查项目、提取 `CHANGELOG.md` 对应版本内容、生成 SHA-256、创建 GitHub Release 并上传安装包。

Release 工作流只接受 `v主版本.次版本.修订号` 格式的稳定版本标签。`CHANGELOG.md` 必须存在对应的 `## v版本号 - 日期` 二级标题且正文不能为空。标签与 `metadata.yaml` 不一致、更新日志缺失、任意测试失败、压缩包完整性检查失败或 GitHub Release 上传失败时，发布任务都会停止。Actions 页面同时保留 30 天的 ZIP、Release 正文和校验文件构建产物。
