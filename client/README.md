# qa-util-client

独立的单账号 Playwright + Tkinter 客户端，负责 UPC 登录、读取论坛状态、提问与回答。
通过 HTTP 领取服务端操作；无需安装 Flask、Waitress 或配置 LLM。

```sh
uv sync --locked --no-dev --managed-python
uv run --no-dev playwright install chromium
cp -n client.example.json client.json
uv run --no-dev qa_util --config client.json
```

填写服务地址、本账号接入码及论坛账号密码。默认读取启动目录的 `client.json`；
`--config` 可指定其他文件。数据目录相对配置文件解析，保存登录状态、发送间隔、未回报操作和日志。
Python 需要提供 Tkinter。一个客户端仅处理一个账号，各实例使用独立数据目录。

点击开始后持续领取任务；暂无可答题时每 3 秒轮询，全部完成后仍等待新任务。
个人回答目标按论坛“我的回答”累计数抵扣，界面显示当前数与目标。
个人达标后继续帮助题目达到回答阈值；提问、个人回答和题目阈值全部达标才完成。
验证码由用户在浏览器内完成，心跳继续；断网暂停发送，恢复后核对网页并回报。
停止会保存状态，重启先核对未完成操作。日志通过界面的“打开日志”查看。

开发测试：`uv run pytest`。构建：`uv build`，可单独分发本目录或构建出的安装包。

## 独立桌面程序

在本目录运行：

```sh
uv sync --locked --group build --managed-python
uv run --locked --group build python build.py
```

`build.py` 准备与当前 Playwright 匹配的 Chromium，再按 `qa_util.spec` 打包 Python、Tkinter 和客户端。
优先复用标准缓存中已安装的匹配版本，缺失时才下载；只包含可见浏览器，省略无界面的 headless shell。
PyInstaller 放在独立 `build` 依赖组，不进入客户端运行依赖或服务端环境。

macOS 产物为 `dist/qa_util.app`，将 `dist/client.example.json` 复制为同级的 `client.json` 后填写自己的配置。
Windows 为 `dist/qa_util/qa_util.exe`，Linux 为 `dist/qa_util/qa_util`；请分发整个目录，配置放在可执行文件旁。
也可在界面中选择其他配置，或使用 `--config /配置路径/client.json`。
构建只携带示例配置，真实凭据、登录状态和日志仍保存在配置指定的外部目录。
两个命令在对应系统上执行；当前构建使用本机 CPU 架构，不进行跨系统或跨架构编译。
