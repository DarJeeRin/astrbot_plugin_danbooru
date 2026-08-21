# AstrBot Danbooru 图片搜索插件

一个面向 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的 Danbooru 搜图插件。支持中英文标签解析、普通图片、可选 R18、独立漫画搜索、命令级最低评分过滤，以及尽量减少重复的广域随机策略。

## 功能

- 支持 Danbooru 官方英文 tag，以及可配置的中文/日文标签对照。
- 普通搜图仅返回 General 与 Sensitive 评级。
- R18 搜图为独立可选命令，默认关闭，仅返回 Questionable 与 Explicit。
- 漫画使用独立 `/danbooru_comic` 命令，不会被普通图片的漫画排除规则误杀。
- 使用命令末尾的 `:数字` 设置最低 score；score 只在插件本地过滤，不发送给 Danbooru API。
- 优先使用 Danbooru `random:N` 扩大随机范围；tag 额度占满时改用随机 ID 游标。
- 为每组查询记录近期图片 ID，降低连续搜索相同 tag 时的重复率。
- 自动验证英文 tag、处理官方 alias，并在中文词有歧义时列出候选。
- 只下载常见静态图片格式，支持文件大小限制、用户冷却和本地缓存清理。

## 兼容性

- AstrBot：`>=4.26,<5`
- Python 依赖：`httpx>=0.27.0`
- 图片发送依赖当前 AstrBot 平台适配器的图片消息能力。

## 安装

### 从 GitHub 安装

在 AstrBot WebUI 的插件管理页面中，使用下面的仓库地址安装：

```text
https://github.com/DarJeeRin/astrbot_plugin_danbooru
```

### 手动安装

将仓库克隆到 AstrBot 的 `data/plugins` 目录，然后重启 AstrBot 或重载插件：

```bash
cd AstrBot/data/plugins
git clone https://github.com/DarJeeRin/astrbot_plugin_danbooru.git
```

AstrBot 会依据 `requirements.txt` 安装依赖；若你的部署方式不自动处理依赖，请在 AstrBot 所用的 Python 环境中安装：

```bash
pip install -r requirements.txt
```

## 命令

| 命令 | 作用 | 示例 |
| --- | --- | --- |
| `/danbooru [tag…][:分数]` | 搜索 General + Sensitive 图片 | `/danbooru hatsune_miku solo:150` |
| `/danbooru_r18 [tag…][:分数]` | 搜索 Questionable + Explicit 图片，默认关闭 | `/danbooru_r18 hatsune_miku:100` |
| `/danbooru_comic [tag…][:分数]` | 搜索带 `comic` tag 的普通评级漫画 | `/danbooru_comic touhou:50` |
| `/danbooru_tags 关键词` | 查询官方英文 tag 或中文候选 | `/danbooru_tags 初音未来` |
| `/danbooru_help` | 显示普通帮助 | `/danbooru_help` |
| `/danbooru_help admin` | 管理员查看别名维护帮助 | `/danbooru_help admin` |

### 最低评分

最低分必须写在整条命令末尾，使用英文冒号：

```text
/danbooru hatsune_miku:150
/danbooru hatsune_miku solo:150
/danbooru_comic touhou:50
```

- 未指定分数时默认为 `0`。
- `:150` 表示只接受 score 大于或等于 150 的结果。
- score 不会加入 API 查询，只会在下载前本地筛选。
- 不支持用户直接传入 `score:`、`rating:`、`order:` 等 Danbooru metatag。

### 多 tag 搜索规则

多个 tag 使用空格分隔，语义是同时满足，也就是 AND：

```text
/danbooru hatsune_miku solo
```

Danbooru 的搜索额度与账号等级有关：

- 未登录用户与 Member：最多 2 个普通 tag。
- Gold：最多 6 个普通 tag。
- Platinum 及以上：Danbooru 不限制普通 tag 数；本插件仍设有最多 10 个的保护上限。
- `rating`、`status`、`score` 等免费 metatag 不占上述额度。
- 负 tag 会占 Danbooru 的额度；本插件不允许用户直接传入负 tag，而是使用本地排除配置。
- `/danbooru_comic` 固定占用 `comic` 这 1 个普通 tag，因此默认额度为 2 时，用户还能附加 1 个 tag。

若配置了 Danbooru 用户名和 API Key，请将 `max_api_query_terms` 调整到账号等级允许的数值；未同时配置这两项时，请保留默认值 `2`。

## R18 搜索

R18 命令默认关闭。管理员需要在插件配置中开启：

```text
enable_r18_search = true
```

开启后使用 `/danbooru_r18`。普通 `/danbooru` 和 `/danbooru_comic` 不会返回 Questionable 或 Explicit 内容。

请根据部署地区、聊天平台规则、群组管理要求和使用者年龄采取合适的访问控制。插件本身只提供总开关，不代替平台侧权限管理。

## 中文标签与别名

中文/日文输入默认通过标签对照服务映射为 Danbooru 官方 tag，不经过 LLM 猜测。若同一个中文词对应多个热门 tag，插件会返回候选，不会静默选择。

默认对照服务：

```text
https://tagsuggest.zeabur.app/api/tags/suggest
```

可关闭 `enable_chinese_lookup`，或通过 `chinese_lookup_url` 更换服务。使用外部服务时，输入的中文关键词会发送给该服务。

管理员别名命令：

| 命令 | 作用 |
| --- | --- |
| `/danbooru_alias 中文 英文tag [英文tag…]` | 添加本地别名 |
| `/danbooru_alias_del 中文` | 删除本地别名 |
| `/danbooru_alias_list [关键词]` | 查看本地别名 |
| `/danbooru_suggest_log [条数]` | 查看最近的中文候选与歧义日志 |

可用 `alias_admin_ids` 配置允许维护别名的用户 ID。平台能够识别管理员身份时，管理员也可直接使用这些命令。

本地数据位于：

```text
data/danbooru/manual_aliases.json
data/danbooru/alias_suggestions.jsonl
```

## 随机策略

插件不再固定从最新结果的高分前 20 张中抽取：

1. 当普通 tag 额度有剩余时，在一次 posts API 请求中使用 `random:N` 获取广域随机池。
2. 当普通 tag 已占满账号额度、无法再加入 `random:N` 时，使用 `page=b<ID>` 在该查询的历史 ID 范围内随机切片。
3. 随机历史页为空时，最多回退一次到该查询的最新页。
4. 本地过滤完成后，优先选择近期没有发送过的 post ID。

默认每次请求 100 条、每组查询记忆最近 200 张；可分别通过 `result_pool_size` 和 `recent_history_size` 调整。tag 校验另有缓存，默认 15 分钟，用于降低 tags API 的重复调用。

## 主要配置

| 配置项 | 默认值 | 说明 |
| --- | ---: | --- |
| `danbooru_username` | 空 | Danbooru 用户名 |
| `danbooru_api_key` | 空 | 与用户名一起启用 Basic Auth |
| `max_api_query_terms` | `2` | posts 搜索可用的普通 tag 数 |
| `enable_r18_search` | `false` | 是否启用 `/danbooru_r18` |
| `result_pool_size` | `100` | 单次随机结果池，范围 1～200 |
| `recent_history_size` | `200` | 每组查询的近期去重数；0 为关闭 |
| `default_positive_tags` | 空 | 本地必须包含的 tag，不发送给 API |
| `default_negative_tags` | 见 schema | 本地排除的 tag，不发送给 API |
| `exclude_animated` | `true` | 排除动画与视频类结果 |
| `max_file_size_mb` | `20` | 最大允许图片大小 |
| `user_cooldown_seconds` | `20` | 同一用户的请求冷却；0 为关闭 |
| `cache_hours` | `24` | 已下载图片的缓存保留时间 |
| `enable_chinese_lookup` | `true` | 启用中文/日文对照 |
| `tag_cache_ttl_seconds` | `900` | 标签校验缓存时间 |
| `show_query_tags` | `true` | 在回复中显示验证标签与实际查询 |

完整配置及 WebUI 提示见 [`_conf_schema.json`](./_conf_schema.json)。

## 常见问题

### 搜索不到图片

- 去掉或降低末尾最低分。
- 检查多个 tag 的组合是否真的存在交集。
- 检查 `default_positive_tags` 和 `default_negative_tags` 是否过严。
- 使用 `/danbooru_tags` 确认 tag 的官方拼写。

### API 返回 422

通常是搜索项超过账号额度。未登录或 Member 请把 `max_api_query_terms` 保持为 `2`；漫画命令已经占用其中一个额度。

### R18 命令提示未启用

在 AstrBot 插件配置中开启 `enable_r18_search`，然后重载插件。

### 图片记录存在但发送失败

检查平台适配器是否支持本地图片发送、文件大小限制以及 AstrBot 到 Danbooru CDN 的网络连通性。

## 项目文件

```text
astrbot_plugin_danbooru/
├── main.py
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt
└── README.md
```

## 相关链接

- [AstrBot](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot 插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html)
- [AstrBot 插件发布指南](https://docs.astrbot.app/dev/star/plugin-publish.html)
- [Danbooru API 帮助](https://shima.donmai.us/wiki_pages/help%3Aapi)
- [Danbooru 搜索语法](https://safebooru.donmai.us/wiki_pages/help%3Acheatsheet)

## 免责声明

本插件与 AstrBot、Danbooru 及标签对照服务的运营方无隶属关系。图片版权归原作者或权利人所有；使用者应遵守 Danbooru 服务条款、聊天平台规则及所在地法律法规。
