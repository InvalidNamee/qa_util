# qa-util-server

独立的 Flask 服务端，负责账号接入码、论坛任务、配额、内容生成和操作分配。
无需安装客户端、Chromium 或 Tkinter；论坛凭据和登录状态由客户端保存。

```sh
uv sync --locked --no-dev --managed-python
cp -n .env.example .env
uv run --no-dev qa_util_server --host 127.0.0.1 --port 8000
```

启动后终端会显示 `Serving on http://127.0.0.1:8000`，用浏览器打开该地址进入管理页。
服务会持续占用终端等待请求，按 `Ctrl+C` 停止。

在本目录 `.env` 中填写 LLM 设置、`SERVER_ADMIN_PASSWORD` 和随机的 `SERVER_SECRET_KEY`。
默认读取启动目录的 `.env`，也可用 `--env-file /路径/.env` 指定。
`server-data/` 默认位于该配置旁；`SERVER_DATA_DIRECTORY` 的相对路径也以配置目录为基准。
`PUBLISH_INTERVAL_SECONDS` 默认 10 秒；`GENERATION_PROMPT` 补充生成要求。

浏览器打开服务地址，登录后登记账号、生成接入码、创建并启动任务。
任务统一设置每号提问目标、每号回答目标和每题回答阈值；个人回答目标不大于提问目标，已有“我的回答”抵扣。
个人两项目标和每题阈值全部满足才完成，个人达标后仍会补齐题目阈值。
管理页可创建、修改账号别名，查看各账号累计提问和回答进度；同时显示原账号，接入码仍绑定原账号。
登记、别名、接入码和删除通过弹窗操作。删除账号会撤销接入码、清理其关联题池与记录并暂停相关运行任务；
已进入提交阶段的操作须先核对完成。删除不会向论坛发送删除请求，原账号之后可重新登记。
支持暂停、恢复和重试失败。旧数据库启动时自动补字段，旧任务保留原规则；新目标通过新建任务设置。
接入码只在生成或重置时显示。部署只运行一个 Waitress 进程，远程访问使用 HTTPS 反向代理。

开发测试：`uv run pytest`。构建：`uv build`，可单独分发本目录或构建出的安装包。
