<div align="center">

# AstrBot JM 漫画下载器

_✨ 在群聊或私聊中通过 JM 号下载本子，自动合并 PDF 并发送加密压缩包 ✨_

<img src="https://img.shields.io/badge/AstrBot-Plugin-blue" alt="AstrBot Plugin">
<img src="https://img.shields.io/badge/AstrBot-4.26%2B-blueviolet" alt="AstrBot 4.26+">
<img src="https://img.shields.io/badge/Python-3.12%2B-green" alt="Python">
<img src="https://img.shields.io/badge/Version-v1.2.0-brightgreen" alt="Version v1.2.0">
<img src="https://img.shields.io/badge/Archive-AES--256%20ZIP-orange" alt="AES-256 ZIP">

</div>

## 插件简介

这是一个基于 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的 JM 漫画下载插件。用户发送 `/本子 <jm号>` 后，插件会立即回复受理消息，在后台下载漫画、合并 PDF，并发送带密码的 AES-256 ZIP 压缩包。管理员可以通过 WebUI 查看任务、用量、缓存、镜像状态和失败统计。

```text
JM 号 → 检查缓存与章节 → 下载 → 合并 PDF → 加密压缩 → 发送文件
```

## 功能特性

- 支持 QQ 群聊和私聊，可识别 `jm123456` 或 `123456`。
- 收到命令后立即响应，下载任务在后台执行。
- 自动合并为 PDF，并使用 AES-256 加密 ZIP 发送。
- 支持分别限制单次下载的章节数和图片页数。
- 章节较多时通过合并转发展示列表并等待用户选择。
- 支持下载缓存、重复任务合并和短时间重复发送冷却。
- 支持任务并发、全局队列和单会话任务数量限制。
- 支持用户请求速率、每日下载次数和每日发送流量配额。
- 支持自动发现可用域名、检查真实重定向，并在章节检查或下载失败时切换候选镜像。
- 可在 WebUI 一次性登录并刷新 Cookie，账号和密码不会写入插件配置。
- 可选图片有损压缩与等比例缩放，以减小 PDF 和发送文件体积。
- 支持缓存空间配额、自动过期和最旧优先清理。
- 提供管理监控 WebUI、结构化日志、失败原因统计和配置导入导出。
- AstrBot 管理员或插件配置管理员可取消指定任务或全部任务。
- 可配置域名、Cookie、代理、缓存目录和下载参数。

## 环境要求

- AstrBot `>=4.26,<5`
- Python `>=3.12`
- NapCat/OneBot（`aiocqhttp`）适配器
- 7-Zip、WinRAR 或其他支持 AES ZIP 的解压工具

其他平台可能可以完成普通命令和文件发送，但未纳入当前版本的兼容性承诺。

## 安装方法

### 方式一：AstrBot 插件市场

在 AstrBot 管理面板的插件市场中搜索“JM 漫画下载器”并安装。

### 方式二：Release 安装

在本项目 [Releases](https://github.com/JustMonika24769/astrbot_plugin_jmdownloader/releases) 页面下载最新压缩包，解压到 `AstrBot/data/plugins/astrbot_plugin_jmdownloader/`。确认 `metadata.yaml` 位于该目录根部，然后在插件管理页面启用或重载插件。

### 方式三：仓库安装

在 AstrBot 插件目录中执行：

```bash
git clone https://github.com/JustMonika24769/astrbot_plugin_jmdownloader.git
```

重启 AstrBot，或在插件管理页面重载并启用插件。AstrBot 通常会根据 `requirements.txt` 自动安装依赖。

## 快速配置

安装后打开插件配置页面，至少填写一个不少于 8 个字符的压缩包密码：

```yaml
zip_password: "请填写至少8位的强密码"
```

常用配置如下：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `zip_password` | 空 | 必填，最终 ZIP 的 AES-256 密码。 |
| `archive_dir` | `archives` | 最终压缩包的缓存和发送目录。 |
| `max_chapters_per_download` | `20` | 单次最大章节数，设为 `0` 表示不限。 |
| `max_pages_per_download` | `80` | 单次最大图片页数，设为 `0` 表示不限。 |
| `duplicate_cooldown_seconds` | `30` | 同一会话重复发送冷却时间。 |
| `max_concurrent_downloads` | `1` | 同时执行的下载任务数。 |
| `client.domain` | `jmcomic-zzz.one` | JM 域名，可填写多个备用域名。 |
| `client.cookies` | 空 | 需要登录或访问权限时填写。 |
| `user_daily_download_limit` | `0` | 单用户每日下载次数，`0` 表示不限。 |
| `user_daily_traffic_mb` | `0` | 单用户每日发送流量，`0` 表示不限。 |
| `pdf_compression_enabled` | `false` | 是否压缩 PDF 输入图片。 |
| `cache_max_size_mb` | `0` | ZIP 缓存空间配额，`0` 表示不限。 |
| `cache_expire_days` | `0` | 缓存自动过期天数，`0` 表示永不过期。 |

密码、Cookie 和代理信息不会发送到聊天中。修改密码后，无法使用新密码解密的旧缓存会自动失效。

全部配置项及填写示例请参阅 [完整配置说明](doc/CONFIGURATION.md)。

## 使用方法

### 下载本子

```text
/本子 jm123456
```

也可以省略 `jm`：

```text
/本子 123456
```

插件会先回复任务受理状态，下载完成后发送 `JM123456.zip`。压缩包内包含整合后的 PDF，解压密码为插件配置页中填写的密码。

### 选择章节

当漫画章节数超过 `max_chapters_per_download` 时，插件会发送章节列表。直接在当前会话回复需要下载的章节，例如：

```text
1
1,3,5
1-3,8-10
```

选择数量不能超过章节上限，章节编号不能越界。回复 `取消` 可以放弃选择；选择超时后需要重新发送 `/本子 <jm号>`。

章节数和图片页数分别限制。假设选择的章节共有 120 页，而 `max_pages_per_download` 为 `80`，插件只会按章节顺序下载前 80 页。

### 缓存与重复请求

- 有效缓存会直接发送，无需重新下载。
- 相同任务正在处理时，重复请求会合并到现有任务。
- 同一会话刚收到过相同文件时，冷却期内不会重复发送。
- 部分章节缓存与整本缓存相互独立。

### 管理员取消任务

AstrBot 全局管理员及 `plugin_admin_qq_ids` 中配置的 QQ 号可以使用：

```text
/本子取消 123456
/本子取消 全部
```

### 管理监控页面

在 AstrBot 插件详情页打开“下载管理与监控”，可以：

- 查看活动任务、今日下载次数、流量和用户用量。
- 取消任务、发现并选择 JM 域名、立即检查域名或执行缓存清理。
- 使用 JM 账号一次性登录，将新 Cookie 保存到插件配置。
- 查看结构化事件和失败原因统计。
- 导出脱敏或完整配置，验证并导入 JSON 配置备份。

## 常见问题

### 压缩包无法解压

插件使用 AES-256 ZIP。部分 Windows 资源管理器版本不支持此格式，请使用 7-Zip 或 WinRAR，并输入插件配置页中的密码。

### 配置密码后仍然收到旧文件

先重载插件或重启 AstrBot。插件会验证缓存是否能被当前密码解密；仍有问题时，可删除 `archive_dir` 中对应 JM 号的 ZIP 和 JSON 缓存文件后重试。

### 下载失败或没有生成 PDF

- 检查 JM 号、域名、Cookie 和代理配置。
- 如果配置域名被重定向到新站点，请在管理页面重新发现域名并使用一次性登录刷新 Cookie。
- 确认 AstrBot 所在设备或容器可以访问目标站点。
- 查看 AstrBot 日志中的错误信息。
- 检查任务队列或当前会话是否已达到配置上限。

### 浏览器能访问，但插件提示本子不存在

站点域名可能已重定向，而旧 Cookie 对新站点的登录状态无效。打开“下载管理与监控”的域名页面，先执行自动发现并勾选可用候选域名，再选择目标域名进行一次性登录。插件只保存登录后获得的 Cookie，不保存账号和密码。

### 启用 PDF 压缩后画质下降

PDF 压缩会将图片重新编码为 JPEG。提高 `pdf_jpeg_quality`、增大 `pdf_max_width`，或关闭 `pdf_compression_enabled`；配置变化后旧缓存会自动失效并重新生成。

### 依赖安装失败

在 AstrBot 使用的 Python 环境中执行：

```bash
python -m pip install -r requirements.txt
```

确保执行命令的 Python 环境与 AstrBot 实际使用的环境一致。

## 注意事项

- 本插件不绕过目标站点权限，下载能力依赖站点可用性和用户配置的 Cookie。
- 请遵守目标站点服务条款及所在地法律法规。
- 请勿在群聊、截图或公开仓库中泄露 Cookie、压缩包密码和代理凭据。
- 插件发送的是加密 ZIP，而不是单独发送 PDF。

## 更多文档

- [完整配置说明](doc/CONFIGURATION.md)
- [开发与实现说明](doc/DEVELOPMENT.md)
- [后续功能规划](doc/ROADMAP.md)
- [更新日志](CHANGELOG.md)
- [安全策略](SECURITY.md)

## 相关项目

- [AstrBot](https://github.com/AstrBotDevs/AstrBot)
- [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python)
