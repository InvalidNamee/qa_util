# 问答工具

Python 3.13、uv、Playwright Chromium 和 Tkinter。登录固定使用 UPC 机构 SSO。

```sh
uv sync --managed-python
uv run playwright install chromium
cp config.example.json config.json
uv run comment-util
```

填写 `config.json` 中的账号密码、论坛主页链接和 `llm` API 信息。也可通过
`uv run comment-util --config /完整路径/config.json` 或界面选择其他配置。
`max_concurrency` 控制同时活跃的账号（包括等待验证码的账号），`questions_per_account` 是每号提问目标。
`generation_prompt` 可补充课程主题和内容要求，API 需支持 Chat Completions JSON 输出。
`publish_interval_seconds` 为同一账号相邻两次提问或回答的最短发送间隔，默认 10 秒；旧配置不填也生效。
跨阶段共用间隔，验证码通过后网页自动提交也受约束；等待时可以停止，其他账号继续运行。间隔并非平台公布的安全阈值。

程序先读取各号旧题，批量生成缺少的新题以及每题给其他账号的回答。预览中确认后才开始发布。
所有账号提问达标并各贡献最新 y 道题后，开始互答：每号覆盖其他账号各 y 道题，无回答入口直接跳过并计入覆盖。

遇到验证码时仅暂停对应账号，浏览器页面会置前。请直接在页面完成验证；程序核对是否已经提交后自动继续，
无需点击确认。等待没有时间上限，停止按钮和关闭程序均可结束任务。

`runs/时间/` 保存生成内容、预览、最终题池和结果。`run.log` 与界面一样只记进度；`debug.log` 记录阶段、目标题目、关键接口的状态与耗时、页面脚本错误及完整异常堆栈。
失败时另存 `failure-a序号.json`（含页面 URL、接口和验证码状态）及页面截图。排查时提供该次运行的整个目录即可。
不保存密码或登录状态；重启会重新读取网页状态，
没有断点续跑功能。实际配置和运行产物已加入 `.gitignore`。

```sh
uv run pytest
```

若 Python 缺少 `_tkinter`，使用 uv 管理的 Python 3.13 重新创建环境。
