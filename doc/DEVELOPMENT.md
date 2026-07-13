# 开发与实现说明

## 项目结构

```text
astrbot_plugin_jmdownloader/
├─ main.py                 # AstrBot 命令和事件入口
├─ jm_service.py           # 下载、限制、缓存、PDF 和发送逻辑
├─ _conf_schema.json       # AstrBot 配置页面定义
├─ metadata.yaml           # 插件元数据
├─ requirements.txt        # 运行依赖
├─ requirements-dev.txt    # 开发依赖
├─ scripts/package.ps1     # 7-Zip 打包脚本
├─ tests/                  # 自动化测试
└─ doc/                    # 扩展文档
```

插件运行时使用 `requirements.txt` 安装的 `jmcomic` 包，不依赖仓库中额外拉取的 JMComic-Crawler-Python 源码目录。

## 处理流程

```text
命令输入
  → 规范化 JM 号
  → 检查密码、冷却、重复任务和队列
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

## 安全边界

- JM 号和章节选择有格式及长度限制。
- 用户可见错误和日志摘要会隐藏 Cookie、密码、令牌和代理认证信息。
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
python -m ruff check main.py jm_service.py tests
python -m pytest
python -m py_compile main.py jm_service.py
```

GitHub Actions 会在 Windows 和 Python 3.12 环境执行静态检查、测试、编译及真实 7-Zip 打包。

## 打包

在项目根目录执行：

```powershell
pwsh -File .\scripts\package.ps1
```

默认生成带版本号的安装包，例如：

```text
dist\astrbot_plugin_jmdownloader-v1.0.0.zip
```

指定格式或输出路径：

```powershell
pwsh -File .\scripts\package.ps1 -ArchiveType 7z
pwsh -File .\scripts\package.ps1 -OutputPath .\dist\custom-name.zip
```

脚本通过 Git 和 `.gitignore` 生成文件清单，排除测试、CI 和开发配置，并检查元数据、配置 Schema、必需文件和示例配置中的疑似凭据。旧输出会在打包前删除，避免残留历史文件。

## 发布前检查

1. 更新 `metadata.yaml`、README 和 CHANGELOG 中的版本号。
2. 确认示例配置不包含真实 Cookie、密码或代理凭据。
3. 执行完整测试、静态检查和编译检查。
4. 运行打包脚本并使用 7-Zip 测试压缩包完整性。
5. 确认压缩包根目录包含 `metadata.yaml`，且不包含测试、缓存和本地配置。
6. 创建与版本号一致的 Git tag 和 GitHub Release，并上传安装包。
