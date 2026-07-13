<div align="center">

# AstrBot JM 漫画下载器

_✨ 在群聊或私聊中通过 JM 号下载本子，自动合并 PDF 并发送加密压缩包 ✨_

<img src="https://img.shields.io/badge/AstrBot-Plugin-blue" alt="AstrBot Plugin">
<img src="https://img.shields.io/badge/AstrBot-4.26%2B-blueviolet" alt="AstrBot 4.26+">
<img src="https://img.shields.io/badge/Python-3.12%2B-green" alt="Python">
<img src="https://img.shields.io/badge/Version-v1.0.0-brightgreen" alt="Version v1.0.0">
<img src="https://img.shields.io/badge/Archive-AES--256%20ZIP-orange" alt="AES-256 ZIP">

</div>

## 插件简介

这是一个基于 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的 JM 漫画下载插件，下载模块使用已安装的 [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python) 包。

用户发送 `/本子 <jm号>` 后，插件会立即回复受理消息，并在后台完成以下流程：

```text
JM 号 → 检查缓存与章节 → 按限制下载图片 → 精确合并 PDF → AES-256 ZIP → 发送文件
```

本插件目录内的 `JMComic-Crawler-Python` 目录仅用于开发时查阅源码，插件运行时使用 `requirements.txt` 安装的依赖。

## 功能特性

- **群聊和私聊下载**：支持 `/本子 jm123456` 和 `/本子 123456`。
- **NapCat/OneBot 适配**：v1.0.0 以 AstrBot 4.26 和 `aiocqhttp` 平台为正式支持范围。
- **立即响应**：收到 JM 号后先返回受理状态，下载过程在后台执行。
- **PDF 整合**：将本次实际下载的精确图片清单直接交给 `img2pdf`，避免完整本子扫描绕过章节或页数限制。
- **AES-256 加密压缩包**：最终发送 `JM<id>.zip`，压缩包内只包含一个加密 PDF。
- **严格缓存校验**：验证缓存版本、限制参数、文件结构、加密标志、当前密码和 PDF 文件头。
- **重复任务合并**：相同 JM 号正在处理时不会重复下载，后续请求会加入同一个发送任务。
- **重复发送冷却**：同一会话短时间内重复请求同一个 JM 号时不会重复发送文件。
- **可配置并发**：可以限制同时进行的后台下载任务数量。
- **防滥用队列**：限制全局队列长度和单会话活动请求数，避免刷不同 JM 号耗尽资源。
- **章节选择**：章节数超过单次下载上限时，先列出章节并等待用户选择下载范围。
- **双重强制限制**：下载执行、PDF 输入和缓存校验都会检查章节数与图片页数上限。
- **配置页完整配置**：支持配置域名、Cookie、代理、目录规则、下载线程、上游插件和文件保存目录。

## 安装方法

### 方式一：AstrBot 插件市场

如果插件已经发布到 AstrBot 插件市场，可以在 AstrBot 管理面板中搜索并安装。

### 方式二：手动安装

将本仓库放入 AstrBot 插件目录后重启 AstrBot：

```bash
git clone https://github.com/JustMonika24769/astrbot_plugin_jmdownloader.git
```

然后在 AstrBot 插件管理页面启用插件，并安装依赖：

```bash
python -m pip install -r requirements.txt
```

依赖包括：

- `jmcomic`：JMComic-Crawler-Python 的已安装包。
- `img2pdf`：将章节图片合并为 PDF。
- `pyzipper`：生成带 AES 密码的 ZIP 文件。
- `PyYAML`：安全解析配置页中的 YAML 插件配置。

## 配置说明

所有配置都在 AstrBot 插件配置页面中填写。

v1.0.0 要求 AstrBot `>=4.26,<5`，正式支持 NapCat/OneBot（`aiocqhttp`）适配器。其他平台可能可以完成普通命令和文件发送，但未纳入本版本兼容性承诺。

| 配置项 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `zip_password` | 是 | 空 | 至少 8 个字符的 AES-256 ZIP 密码。不会发送到聊天中；修改后旧缓存自动失效。 |
| `archive_dir` | 否 | 插件数据目录 `archives` | 最终 ZIP 的缓存和发送目录。相对路径按插件数据目录解析。 |
| `duplicate_cooldown_seconds` | 否 | `30` | 同一会话重复请求同一个 JM 号时的发送冷却时间，设为 `0` 关闭。 |
| `max_concurrent_downloads` | 否 | `1` | 最大同时下载数，建议保持 `1`。 |
| `max_queue_size` | 否 | `10` | 正在检查、下载或发送的最大任务总数。 |
| `max_active_requests_per_session` | 否 | `2` | 同一个群聊或私聊同时允许保留的不同任务数。 |
| `max_chapters_per_download` | 否 | `20` | 单次最多下载的章节数。超过时询问章节范围；设为 `0` 表示不限章节数。 |
| `max_pages_per_download` | 否 | `80` | 单次最多下载的图片页数。按章节编号顺序累计，达到上限后停止；设为 `0` 表示不限页数。 |
| `chapter_selection_timeout_seconds` | 否 | `600` | 章节选择等待时间，单位秒，默认 10 分钟。 |
| `client.domain` | 否 | `jmcomic-zzz.one` | JM 域名列表，按顺序填写，不要带协议和路径。 |
| `client.impl` | 否 | `html` | JM 客户端实现，可选 `html` 或 `api`。 |
| `client.retry_times` | 否 | `5` | 请求失败时的重试次数。 |
| `client.cookies` | 否 | 空 | JM Cookie，支持 JSON 或 Cookie 请求头格式。 |
| `client.proxies` | 否 | `{}` | 请求代理，填写 JSON 对象。 |
| `client.impersonate` | 否 | `chrome110` | curl-cffi 浏览器指纹配置。 |
| `dir_rule.base_dir` | 否 | 插件数据目录 `downloads` | JM 下载工作目录。实际任务会在其中创建临时任务目录。 |
| `dir_rule.rule` | 否 | `Bd_Aid_Pid` | JMComic-Crawler-Python 目录规则 DSL。插件会确保内部路径包含 `Pid`，防止多章图片共用目录。 |
| `dir_rule.normalize_zh` | 否 | 空 | 可填写 `zh-cn` 或 `zh-tw`，用于繁简体归一化。 |
| `download.cache` | 否 | `true` | 上游下载器的图片缓存开关。插件自身还会检查最终 ZIP 缓存。 |
| `download.image.suffix` | 否 | `.jpg` | 下载图片的目标后缀。 |
| `download.threading.image` | 否 | `15` | 图片下载并发数。 |
| `download.threading.photo` | 否 | `2` | 章节下载并发数。 |
| `plugins` | 否 | `valid: log` | 高级 JMComic 插件配置，支持 YAML 或 JSON；仅应启用可信插件，`img2pdf` 项由本插件接管。 |

### 推荐配置

至少需要填写压缩包密码：

```yaml
zip_password: "请填写至少8位的强密码"
```

如果默认域名不可用，可以在配置页的 `client.domain` 中填写多个域名：

```yaml
client:
  domain:
    - jmcomic-zzz.one
    - example.com
  impl: html
  cookies: ""
```

通常无需修改 `plugins`，保留以下默认值即可：

```yaml
valid: log
```

为确保章节和页数限制严格生效，配置中的 `after_album/img2pdf` 项会被移除。插件会记录本次实际下载成功的章节对象，按照截断后的页面列表生成精确图片路径清单，再直接调用 `img2pdf`。其他自定义 `after_album` 插件仍会在 PDF 生成后执行。

### PDF 体积说明

`img2pdf` 对标准 JPEG 采用直接嵌入，正常情况下 PDF 体积应接近所有图片大小之和，并不是通过调整 PDF 压缩质量来缩小。旧版默认目录规则 `Bd_Aid` 会让同一本漫画的多个章节共用同一图片目录，上游 `after_album/img2pdf` 又会按章节重复读取该目录，造成图片重复嵌入、页面覆盖以及 PDF 体积按章节数成倍膨胀。

本插件现在会在任务内部自动补充 `Pid` 章节目录，并且不再把完整 album 交给上游 `after_album/img2pdf` 扫描。PDF 只会包含本次实际选择且未超过页数上限的图片。已有缓存清单缺少新版章节数、页数和 PDF 布局标识时会自动失效并重新下载，无需手动删除旧压缩包。

受页数限制的章节只执行一次详情检查。插件会锁定截断后的 `page_arr`，直接调度对应图片，不再调用内部可能再次执行 `check_photo` 的高层章节下载方法；因此客户端或插件回调无法把页面列表恢复为完整章节后继续下载。

## Cookie 填写说明

Cookie 默认留空。可以填写 JSON：

```json
{"AVS":"你的值","remember":"你的值"}
```

也可以填写浏览器请求头中的 Cookie 字符串：

```text
AVS=你的值; remember=你的值
```

Cookie 是 JM 登录态或访问权限凭证，请不要发送到群聊、截图或提交到公开仓库。建议只在 AstrBot 面板中配置。

## 使用方法

### 下载本子

```text
/本子 jm123456
```

或：

```text
/本子 123456
```

插件会先回复：

```text
已收到 JM123456，正在检查章节数并准备下载。
```

下载完成后，机器人会发送：

```text
JM123456.zip
```

压缩包内为整合后的 PDF 文件。解压时需要使用配置页中的密码。

### 选择章节

当本子章节数超过 `max_chapters_per_download` 时，插件会通过合并转发消息发送章节列表，并等待当前会话回复章节范围：

```text
1-3,5
```

支持单章、逗号分隔和连续范围，例如：

```text
1
1,3,5
1-3,8-10
```

选择数量不能超过章节上限，章节编号不能越界。回复 `取消` 可以放弃选择；超出 `chapter_selection_timeout_seconds` 后需要重新发送 `/本子 <jm号>`。

章节选择通过格式、范围和数量校验后，插件会尝试撤回用户发送的选择消息，再发送受理结果。格式错误、章节越界或选择数量超过限制时不会撤回，方便用户对照修改。QQ 群内撤回其他成员的消息需要机器人具有管理员权限；撤回失败不会影响下载任务。

在 NapCat/OneBot 平台中，插件会优先使用原始事件中的整数消息 ID 执行撤回。QQ 的权限规则不允许群管理员撤回群主或其他管理员发送的消息，也不允许机器人撤回对方发送的私聊消息；这些情况会直接跳过撤回。NapCat 可能将群权限不足报告为 `decode failed`，插件会将其转换为明确的权限提示，下载任务不受影响。

章节数和图片页数分别由两个配置控制。假设选择第 `1-3,5` 章，而这些章节合计有 120 页，`max_pages_per_download` 配置为 `80`，插件会按第 1、2、3、5 章的顺序累计下载，只保留前 80 页；达到 80 页时，当前章节的后续页面及后面的章节都不会下载。单章本子同样受此限制。

如果用户选择了部分章节，缓存文件会使用类似 `JM123456-chapters-1_3_5.zip` 的名称，并且不会与整本缓存混用。

### 缓存命中

如果已经存在有效缓存，插件不会再次访问 JM 站点，而是直接发送缓存：

```text
已找到 JM123456 的缓存，正在发送压缩包。
```

插件会同时检查：

1. ZIP 文件是否存在且非空。
2. 缓存文件名、JM 号和缓存格式版本是否一致。
3. 缓存清单中的章节上限和页数上限是否匹配当前配置。
4. 缓存记录的实际章节数、PDF 页数是否未超过当前限制。
5. ZIP 是否只包含一个无路径穿越风险的加密 PDF。
6. 当前密码是否可以解密，并且文件头是否确实为 `%PDF-`。

### 重复请求

同一个 JM 号正在下载时，重复请求不会启动第二个下载任务。不同会话请求同一个 JM 号时，任务完成后会分别发送到对应会话。

同一会话在 `duplicate_cooldown_seconds` 时间内重复请求已发送的文件时，会返回跳过提示，不会重复上传相同文件。

## 压缩包说明

插件使用 `pyzipper` 创建 AES-256 加密 ZIP，而不是普通无密码 ZIP。建议使用以下工具解压：

- 7-Zip
- WinRAR
- 其他支持 AES ZIP 的解压工具

部分 Windows 资源管理器版本不支持 AES ZIP，使用资源管理器测试时可能无法正常提示密码，请改用 7-Zip 或 WinRAR。

## 目录和缓存

默认目录结构如下：

```text
AstrBot 插件数据目录/
├─ archives/
│  ├─ JM123456.zip
│  ├─ JM123456.zip.json
│  ├─ JM123456-chapters-1_3_5.zip
│  └─ JM123456-chapters-1_3_5.zip.json
└─ downloads/
```

`JM<id>.zip.json` 是缓存格式、章节数、页数和限制参数清单，不包含密码或密码散列。下载任务完成后，临时图片和 PDF 工作目录会被清理，最终 ZIP 保留在 `archive_dir` 中。

## 网络和故障排查

### 依赖安装失败

确认 AstrBot 使用的 Python 环境与执行安装命令的 Python 环境一致：

```bash
python -m pip install -r requirements.txt
python -c "import jmcomic, img2pdf, pyzipper; print('dependencies ok')"
```

### 域名访问失败

- 检查 `client.domain` 是否仍然可访问。
- 可以填写多个候选域名。
- 检查 `client.impl` 是否应切换为 `api`。
- 需要登录时填写有效 Cookie。
- 如果 AstrBot 运行在 Docker 中，确认容器可以访问目标域名。

### 下载失败或没有生成 PDF

- 检查 JM 号是否正确。
- 检查 Cookie、域名和代理配置。
- 确认 `img2pdf` 已安装。
- 检查 AstrBot 日志中的 JMComic-Crawler-Python 错误信息。
- 检查任务队列是否已达到 `max_queue_size`，以及当前会话是否达到活动请求上限。

### 配置密码后仍然收到旧文件

请先重载插件或重启 AstrBot。新版本会验证缓存的实际加密状态和密码；如果缓存无效，会自动重新下载。也可以手动删除 `archive_dir` 中对应的 `JM<id>.zip` 和 `JM<id>.zip.json`。

## 打包

在项目根目录执行：

```powershell
pwsh -File .\scripts\package.ps1
```

默认使用本机 7-Zip 生成包含版本号的安装包，例如 `dist\astrbot_plugin_jmdownloader-v1.0.0.zip`。脚本通过 Git 按 `.gitignore` 生成基础文件清单，并排除测试、CI 和开发配置；随后验证元数据、配置 Schema、必需文件和示例配置中的疑似凭据，再删除旧输出并全新打包，避免旧版本残留文件。

也可以指定输出格式和路径：

```powershell
pwsh -File .\scripts\package.ps1 -ArchiveType 7z
pwsh -File .\scripts\package.ps1 -OutputPath .\dist\custom-name.zip
```

开发者提交前应执行：

```powershell
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m ruff check main.py jm_service.py tests
python -m pytest
```

仓库内的 GitHub Actions 会在 Windows 与 Python 3.12 环境自动执行代码检查、测试、编译和真实 7-Zip 打包。

## 注意事项

- 本插件不绕过 JM 站点权限，下载能力依赖目标站点可用性和配置的 Cookie。
- 请遵守目标站点的服务条款和所在地法律法规。
- Cookie、压缩包密码和代理信息属于敏感配置，请妥善保管。
- 示例配置不得填写真实 Cookie；发布脚本发现常见凭据字段时会停止打包。
- 插件默认使用 AES-256 ZIP，发送的是加密压缩包，不是单独发送 PDF。

## 后续规划

以下功能是 v1.x 的候选方向，当前版本尚未实现：

- 下载进度与队列位置查询，以及管理员取消任务命令。
- 缓存列表、缓存空间配额、按 JM 号清理和自动过期策略。
- 群聊白名单、管理员专用模式、用户级速率限制与每日配额。
- 收藏订阅与新章节通知，支持仅下载新增章节。
- 域名健康检查与自动切换可用镜像。
- 可选 PDF 图片压缩、最大文件体积限制和超大文件分卷发送。
- 下载断点续传、失败图片重试和任务恢复。
- WebUI 状态页与结构化运行指标。

## 相关项目

- [AstrBot](https://github.com/AstrBotDevs/AstrBot)
- [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python)
