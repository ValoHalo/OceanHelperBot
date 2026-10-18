# OceanHelperBot

一个可扩展的 Telegram 群组助手。目前提供链接清洗和 GitHub 仓库动态推送。

## 已支持的链接

- 京东：`3.cn`、`item.jd.com`、`item.m.jd.com`、`m.jd.com`
- 淘宝和天猫：`e.tb.cn`、`m.tb.cn`、`m.taobao.com`、淘宝/天猫商品页
- 哔哩哔哩：`b23.tv`、`m.bilibili.com`、哔哩哔哩视频页
- Pixiv：`pixiv.net` → `phixiv.net`，`www.pixiv.net` → `www.phixiv.net`
- X：`x.com` → `fixupx.com`，同时支持 `www.x.com`、`m.x.com`、`mobile.x.com`
- Twitter：`twitter.com` → `fxtwitter.com`，同时支持 `www.twitter.com`、`m.twitter.com`、`mobile.twitter.com`
- 其他 HTTP(S) 长链：删除常见跟踪参数，保留未知的功能参数

已支持站点中具有明确对应关系的手机版页面会转换为 PC 版页面。回复中只包含清洗后的链接。同一条消息中有多个可清洗链接时，每行回复一个。链接没有变化或短链解析失败时不会回复。

淘宝和天猫商品链接保留商品 `id` 和选择商品规格的 `skuid` 参数（包括 `skuId` 写法）。

哔哩哔哩视频链接保留末尾的 `/`、时间跳转参数 `t` 和分 P 参数 `p`，其他查询参数默认删除。机器人回复会启用网页预览；同一条回复包含多个链接时，使用第一个清洗后的链接生成预览。

## 链接规则管理

仅 `OWNER_ID` 可以在与机器人的私聊中管理链接规则，规则对所有会话生效。

| 命令 | 功能 |
| --- | --- |
| `/addlink` | 发送示例链接，设置域名替换及参数白名单 |
| `/replacelink` | 选择站点并修改目标域名 |
| `/whitelist` | 选择站点并编辑参数白名单 |
| `/links` | 查看规则、启用或停用、删除、恢复内置默认值和撤销 |
| `/cancel` | 退出当前操作，丢弃未保存的修改 |

也可以直接带参数进入预览或编辑：

```text
/addlink https://example.com/item?id=42&ref=share
/replacelink x.com fixupx.com
/whitelist www.bilibili.com p t
/whitelist x.com --clear
```

**白名单严格按参数名放行：只有名单中的参数保留，其他全部删除。空名单删除全部查询参数。** 参数值、重复参数和空值保留；名称区分大小写，淘宝/天猫的 `skuid` 兼容原有大小写写法。清理不会移除 URL 的 `#fragment`。新站点的白名单初始为空，示例中出现的参数不会自动获得保留权限。

参数可以通过按钮勾选，也可以输入完整名单（空格或换行分隔，替换当前草稿名单）。先预览，再点击保存，规则才会立即生效。替换目标只接受域名，路径与允许的参数保留。“不额外替换”会保留解析后的域名，短链展开、商品页或视频页规范化仍会执行。已有站点的手机版、短链等包含域名共用同一条规则。

需要输入时，请使用 Telegram 的回复操作回答机器人指定的提示消息；其他聊天内容不会当作配置。按钮更新原管理消息，旧菜单和旧按钮失效。返回编辑保留草稿；开始新的管理命令、取消、30 分钟无操作或重启会结束本次编辑。保存、删除后可撤销；重启后可从 `/links` 撤销尚无后续修改的记录。恢复默认也需要预览和保存。

未配置站点仍使用通用跟踪参数清理。已配置站点完全按其白名单执行，允许的参数不会被旧跟踪参数规则再次删除。停用规则后，该规则覆盖的链接保持原样；删除规则后，这些域名回到通用处理。短链解析失败时不回复处理结果。

### 初始白名单与数据迁移

| 站点 | 初始允许的参数 |
| --- | --- |
| 京东商品 | 无 |
| 京东主页及其手机版 | `keyword`、`page` |
| 淘宝、天猫商品 | `id`、`skuid`（兼容 `skuId`） |
| 淘宝主页及其手机版 | `q`、`page` |
| 哔哩哔哩 | `p`、`t` |
| Pixiv | `illust_id`、`mode`、`id`、`p`、`word`、`lang` |
| X、Twitter | `lang`、`q`、`f` |

首次启动自动把现有站点、包含域名、域名替换和上述白名单导入 `data/link_cleaner.sqlite3`。导入在一个数据库事务中完成，失败会回滚。数据库记录迁移版本，后续启动不会覆盖用户修改，也不会重新添加已删除的规则。旧版允许未知参数的站点，升级后会按表中的严格白名单删除其余参数；可通过 `/whitelist` 调整。

链接数据库单独保存规则、示例链接和修改历史，不修改 GitHub 的订阅数据库或检查进度。可通过 `.env` 中的 `LINK_CLEANER_STATE_PATH` 自定义位置，不能与 GitHub 数据库使用同一文件。路径默认相对于启动目录；附带启动脚本会切换到项目目录。

备份或迁移时，停止机器人后复制 `data` 目录和 `.env`；它们不应提交到 Git。恢复时使用与数据库版本兼容的程序。升级前同时保留原程序及数据备份；回退旧程序会恢复旧版写在代码中的链接规则，不会读取新链接数据库中的修改。

## 安装

需要 Python 3.11 或更高版本。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

向 Telegram 的 `@BotFather` 创建机器人并取得 token。将 `.env.example` 复制为 `.env`，然后填写：

```dotenv
TELEGRAM_BOT_TOKEN=123456789:replace-with-real-token
OWNER_ID=123456789
PROXY_URL=socks5://127.0.0.1:1080
GITHUB_TOKEN=replace-with-your-github-token
GITHUB_WATCH_INTERVAL_SECONDS=300
GITHUB_STATE_PATH=data/github_watcher.sqlite3
LINK_CLEANER_STATE_PATH=data/link_cleaner.sqlite3
```

`OWNER_ID` 是唯一可以管理 GitHub 订阅的 Telegram 用户 ID。其他用户不能添加、查看或删除订阅，也不能操作仓库选择按钮。可通过 Telegram 的用户信息机器人查询自己的数字 ID。

`PROXY_URL` 可留空。配置后，Telegram Bot API、long polling、GitHub API 和链接解析请求都会使用该代理；支持 `http://`、`https://` 和 `socks5://` 代理地址，也可以在地址中填写用户名和密码。

按钮式仓库选择需要配置 `GITHUB_TOKEN`。使用原有文本命令关注公开仓库时可以留空。配置 fine-grained personal access token 后也可读取 token 有权访问的私有仓库。轮询间隔默认 300 秒，不能低于 30 秒。订阅和检查进度默认保存在 `data/github_watcher.sqlite3`。

如果机器人需要读取群里的普通消息，还要在 `@BotFather` 中执行 `/setprivacy`，选择该机器人并关闭 Privacy Mode，然后把机器人加入目标群组。

机器人可直接用于启用了话题模式的群组。它会在收到消息的同一话题内回复，不会把结果发送到其他话题或群组的常规消息区。

## 运行

```powershell
ocean-helper-bot
```

也可以直接运行模块：

```powershell
python -m ocean_helper_bot
```

当前使用 long polling，不需要公网地址或 webhook。

发送 `/health` 可检查机器人是否正在运行；机器人正常时会回复“运行正常。”。该命令不涉及管理操作，所有用户均可使用。

## GitHub 仓库动态

只有 `OWNER_ID` 对应的用户可以管理订阅。在希望接收动态的群组、私聊或话题中执行：

```text
/newintegration
```

机器人会列出 `GITHUB_TOKEN` 有权访问且当前会话尚未关注的仓库，通过按钮选择即可。查看或删除当前会话的订阅：

```text
/listintegrations
/delintegration
/cancel
```

也可以继续使用原有的文本命令：

```text
/github_follow owner/repo
```

首次关注只记录当时最新的默认分支 commit 和已发布 release，不补发历史。此后检测到的新动态会主动发送到执行命令的同一会话；如果命令来自话题，则发送到同一话题。

`/github_follows` 和 `/github_unfollow owner/repo` 仍然可用。

也可以把完整的 GitHub 仓库地址传给关注和取消关注命令。

## 扩展功能

每项群组功能放在 `ocean_helper_bot/features/` 的独立目录中，并暴露 `register(application)`。在 `ocean_helper_bot/features/__init__.py` 注册新功能即可。

普通站点通过 `/addlink` 添加即可。如果需要新的短链解析方式，在 `link_cleaner/processors.py` 中实现解析器，将其注册到 `service.py` 的 `RESOLVERS`，并在规则中关联解析器。解析器应保留解析结果的查询参数，统一由规则白名单执行过滤。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试使用模拟 Telegram/HTTP 请求和临时 SQLite 数据库，不发送真实消息或修改运行数据。

## License

GPL-3.0-or-later
