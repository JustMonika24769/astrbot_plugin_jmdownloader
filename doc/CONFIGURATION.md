# 完整配置说明

插件配置由 AstrBot 管理页面提供。首次使用至少需要设置 `zip_password`，其他配置可以保持默认值。

## 基础配置

| 配置项 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `zip_password` | 是 | 空 | 至少 8 个字符的 AES-256 ZIP 密码。不会发送到聊天中；修改后旧缓存自动失效。 |
| `archive_dir` | 否 | 插件数据目录 `archives` | 最终 ZIP 的缓存和发送目录。相对路径按插件数据目录解析。 |
| `duplicate_cooldown_seconds` | 否 | `30` | 同一会话重复请求同一个 JM 号时的发送冷却时间，设为 `0` 关闭。 |
| `max_concurrent_downloads` | 否 | `1` | 最大同时下载数，建议保持 `1`。 |
| `max_queue_size` | 否 | `10` | 正在检查、下载或发送的最大任务总数。 |
| `max_active_requests_per_session` | 否 | `2` | 同一个群聊或私聊同时允许保留的不同任务数。 |
| `max_chapters_per_download` | 否 | `20` | 单次最多下载的章节数；超过时询问章节范围，设为 `0` 表示不限。 |
| `max_pages_per_download` | 否 | `80` | 单次最多下载的图片页数；按章节顺序累计，设为 `0` 表示不限。 |
| `chapter_selection_timeout_seconds` | 否 | `600` | 等待章节选择的时间，单位为秒。 |

## JM 客户端配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `client.domain` | `jmcomic-zzz.one` | JM 域名列表，按顺序填写，不要带协议和路径。 |
| `client.impl` | `html` | 客户端实现，可选 `html` 或 `api`。 |
| `client.retry_times` | `5` | 请求失败时的重试次数。 |
| `client.cookies` | 空 | JM Cookie，支持 JSON 或 Cookie 请求头格式。 |
| `client.proxies` | `{}` | 请求代理，填写 JSON 对象。 |
| `client.impersonate` | `chrome110` | curl-cffi 浏览器指纹配置。 |

默认域名不可用时，可以配置多个候选域名：

```yaml
client:
  domain:
    - jmcomic-zzz.one
    - example.com
  impl: html
```

### Cookie 格式

Cookie 默认留空。可以填写 JSON：

```json
{"AVS":"你的值","remember":"你的值"}
```

也可以填写浏览器请求头中的 Cookie 字符串：

```text
AVS=你的值; remember=你的值
```

Cookie 属于登录态或访问凭据，只应保存在 AstrBot 配置页面中。

### 代理格式

```json
{"http":"http://127.0.0.1:7890","https":"http://127.0.0.1:7890"}
```

## 用户配额与管理员

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `user_rate_limit_requests` | `5` | 单用户在速率窗口内最多提交的下载命令数，`0` 表示关闭。 |
| `user_rate_limit_window_seconds` | `60` | 用户速率限制窗口。 |
| `user_daily_download_limit` | `0` | 单用户每天成功接收的 ZIP 数量，缓存发送也计入，`0` 表示不限。 |
| `user_daily_traffic_mb` | `0` | 单用户每天成功接收的 ZIP 总大小，`0` 表示不限。 |
| `plugin_admin_qq_ids` | `[]` | 可以使用 `/本子取消` 的额外 QQ 号。 |

下载次数和流量在文件发送前按实际 ZIP 大小原子预留；平台发送失败时会自动回滚。AstrBot 全局 `admins_id` 管理员无需重复填写到插件管理员列表。

管理员命令：

```text
/本子取消 123456
/本子取消 全部
```

## 域名健康检查

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `domain_health_check_enabled` | `true` | 定期检查域名并自动调整使用顺序。 |
| `domain_health_check_interval_minutes` | `30` | 自动检查间隔。 |
| `domain_health_check_timeout_seconds` | `10` | 单个域名的请求超时。 |

健康域名按延迟从低到高排列，未检查域名保持配置顺序，不可用域名放到最后。全部健康检查结果不会替代 JMComic 自身的域名重试机制。

## PDF 图片压缩

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `pdf_compression_enabled` | `false` | 生成 PDF 前将图片重新编码为 JPEG。 |
| `pdf_jpeg_quality` | `75` | JPEG 质量，推荐 70 至 85。 |
| `pdf_max_width` | `1600` | 超宽图片等比例缩小到该宽度，`0` 表示不缩放。 |

启用后通常可以显著减小文件体积，但会增加 CPU、内存占用并产生有损画质。PNG 透明背景会转换为白色。压缩参数变化时，不匹配的新旧缓存不会混用。

## 缓存与统计保留

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `cache_max_size_mb` | `0` | 最终 ZIP 缓存空间配额，超过后从最旧文件开始删除。 |
| `cache_expire_days` | `0` | 缓存距最后成功使用的过期天数，`0` 表示永不过期。 |
| `cache_cleanup_interval_minutes` | `60` | 自动执行缓存检查和清理的间隔。 |
| `structured_log_retention_days` | `30` | 结构化任务事件、失败原因和用量数据保留天数。 |

活动任务正在使用的文件不会被缓存清理删除。管理页面可以手动执行一次清理。

## 配置导入与导出

管理页面默认导出不包含 `zip_password`、Cookie 和代理的脱敏 JSON。导入脱敏配置时，当前敏感配置保持不变；完整导出需要明确确认，并应视为敏感凭据文件妥善保管。

导入前会检查：

- 插件和导出格式版本。
- 配置字段名称、类型、取值范围和可选值。
- 密码长度、域名列表、Cookie/代理 JSON 和管理员 QQ 格式。
- 是否存在正在运行的任务；活动任务结束或取消前不会应用新配置。

## 下载与目录配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `dir_rule.base_dir` | 插件数据目录 `downloads` | 下载任务的临时工作目录。 |
| `dir_rule.rule` | `Bd_Aid_Pid` | 上游目录规则；插件会确保包含章节目录，防止图片混用。 |
| `dir_rule.normalize_zh` | 空 | 可填写 `zh-cn` 或 `zh-tw`。 |
| `download.cache` | `true` | 上游图片缓存开关，独立于最终 ZIP 缓存。 |
| `download.image.suffix` | `.jpg` | 下载图片的目标后缀。 |
| `download.threading.image` | `15` | 图片下载并发数。 |
| `download.threading.photo` | `2` | 章节下载并发数。 |
| `plugins` | `valid: log` | 高级 JMComic 插件配置，支持 YAML 或 JSON。仅应启用可信插件。 |

通常无需修改 `plugins`，保留默认值即可：

```yaml
valid: log
```

为了保证章节和页数限制生效，插件会接管 PDF 生成并移除 `after_album/img2pdf` 配置。其他自定义插件仍可能影响下载行为，因此只应启用来源可信且经过验证的插件。

## 章节和页数限制

两个限制相互独立：

- `max_chapters_per_download` 控制用户一次最多选择多少章。
- `max_pages_per_download` 控制最终最多下载多少张图片并生成多少页 PDF。

例如用户选择第 `1-3,5` 章，共 120 页，而页数上限为 80，插件会按照第 1、2、3、5 章的顺序累计，只保留前 80 页。

## 缓存目录

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

JSON 文件用于记录缓存版本、章节数、页数和限制参数，不包含密码或密码散列。临时图片和 PDF 会在任务结束后清理，最终 ZIP 保留在 `archive_dir` 中。
