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
