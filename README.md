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

哔哩哔哩视频链接保留末尾的 `/`、时间跳转参数 `t`、分 P 参数 `p` 和其他非跟踪参数。机器人回复会启用网页预览；同一条回复包含多个链接时，使用第一个清洗后的链接生成预览。

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

新增链接站点时，在 `link_cleaner/processors.py` 中实现 `LinkProcessor`，再把实例加入 `PROCESSORS`。站点专用处理器应放在通用参数清洗器之前。

## License

GPL-3.0-or-later
