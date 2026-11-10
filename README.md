# qa_util 问答工具

Python 3.13、uv。服务端与单账号客户端已拆成两个独立安装包，各有自己的 `pyproject.toml`、
`uv.lock` 和 `.venv`，可分别复制到不同机器运行。

| 目录 | 安装包 / 启动命令 | 职责 |
| --- | --- | --- |
| [server/](server/README.md) | `qa-util-server` / `qa_util_server` | Flask 管理任务、配额、LLM 生成和操作分配 |
| [client/](client/README.md) | `qa-util-client` / `qa_util` | Tkinter 界面、单账号 HTTP 登录和论坛操作 |

服务端无需浏览器或 Tkinter；客户端无需 Flask、dotenv 或 LLM 设置。两端通过 HTTP 通信，没有 Python 包互相依赖。

## 安装和运行

服务端，在仓库根目录执行：

```sh
uv sync --project server --locked --no-dev --managed-python
cp -n server/.env.example server/.env
uv run --directory server --no-dev qa_util_server
```

先在 `server/.env` 填写 LLM 设置、管理密码和会话密钥。默认服务地址 `http://127.0.0.1:8000`。
登录管理页，登记账号、生成独立接入码，创建任务并勾选参与账号后启动。
每个任务统一设置每号提问目标、每号回答目标和每题回答阈值；每号回答目标为正整数且不大于提问目标。
账号可填写和修改管理别名，管理页同时显示原账号以便核对。任务暂停后可编辑参数，保存后手动恢复，继续已有进度。
服务采用 Waitress 单进程和一个生成线程，远程访问使用 HTTPS 反向代理。

客户端，在仓库根目录执行：

```sh
uv sync --project client --locked --no-dev --managed-python
cp -n client/client.example.json client/client.json
uv run --directory client --no-dev qa_util --config client.json
```

客户端填写服务地址、账号接入码及论坛账号密码。论坛密码、Cookie 和登录状态仅保存在客户端。
每个客户端只绑定一个账号，自动轮转处理参与的任务；暂无操作时每 3 秒轮询，心跳每 10 秒发送。
Python 需提供 Tkinter；各实例使用独立数据目录。

## 执行行为

累计提问抵扣目标，首次选取最新旧题，不足才补齐，登记后的题池固定。
回答按网页总数达标：已有 3 条、目标 5 条就补 2 条。排除作者和本人已回答的问题，同题一次只补一条。
个人回答按论坛“我的回答”累计数抵扣，个人提问、个人回答和题池中每题阈值全部达标才完成。
优先回答未达阈值的题；个人回答不足时也能回答已达阈值的题，个人达标后继续补齐题目阈值。
没有入口只跳过该账号与该题的组合，不虚增回答数；没有合适账号时继续等待。

服务端自动生成和下发内容，无生成预览确认。发送默认间隔 10 秒，可在服务端 `.env` 调整。
论坛查询和发布使用 curl_cffi。遇到验证码或风控时暂停提交并维持心跳，在论坛人工处理后停止并重新开始。
断网暂停新发送，恢复或重启后先通过论坛接口核对，再回报结果。
管理页暂停任务后不再分配新操作，提交中的操作完成核对。客户端完成后保持在线等待新任务。

## 配置与迁移

原有真实 `.env`、`config.json`、`client.json`（如有）、`server-data/`、`client-data/` 和 `runs/` 均保留在原位置。
继续使用根目录服务端配置和数据时执行：

```sh
uv run --directory server --no-dev qa_util_server --env-file ../.env
```

`--env-file` 默认指向启动目录的 `.env`；服务端数据目录相对该文件所在目录解析。
现有客户端配置可通过 `--config /原路径/client.json` 加载，数据目录仍相对客户端配置解析。
旧多账号 `config.json` 不直接用于新客户端；将论坛和配额填入管理页，每人自己的凭据填入单号配置。
真实配置、登录状态、数据库和日志不进入安装包或版本控制。备份数据库前先停止服务。
升级后重启服务端。服务端自动补齐数据库字段；旧任务不追加个人回答要求，可暂停后编辑设置新目标。

## 开发验证

根目录只提供联合开发和集成测试环境，部署时分别使用两个子项目即可。

```sh
uv sync --locked --managed-python
uv run pytest
```

分别在 `server/` 或 `client/` 下执行 `uv run pytest` 可独立测试，执行 `uv build` 可独立构建安装包。
