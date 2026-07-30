# 网上大学课程自动化工具

统一处理登录、课程链接收集、挂课、AI 考试、人工考试和课程资料保存。主菜单显示当前账号、账号有效期、队列数量和建议操作。

## 安装与启动

```bash
uv venv
uv pip install -r requirements.txt
```

复制 `.env.example` 为 `.env`，双击 `run.bat`，或运行：

```bash
uv run launcher.py
# 或
python launcher.py
```

## 目录结构

```text
course-afk/
  launcher.py          # 入口
  run.bat
  core/                # 业务源码（按领域分包）
    auth/              # 登录与凭证
    browser/           # Playwright 会话与页面
    queues/            # 课程/考试 JSON 队列
    learning/          # 挂课流程
    exam/              # AI / 人工考试
    discovery/         # 主题/培训班/资料收集
    app/               # 工作流与菜单控制
    ui/                # CLI + Textual TUI
  data/                # 运行时数据（gitignore）
    credentials/       # Cookie 与凭证元数据
    links/             # 课程、考试及失败队列
    logs/              # 分级日志与历史归档
    references/        # 课程参考资料
  tests/
  tools/               # 诊断脚本；结果分类归档到 capture/（gitignore）
```

首次启动只会创建当前版本的分类目录。旧版平铺在 `data/` 下的 JSON、`log.log` 和 `参考资料/` 不再识别，也不会自动迁移。

## 输出文件与状态

均位于 `data/` 的分类目录：

- `data/links/课程链接.json`：待处理的课程和主题。
- `data/links/挂课失败链接.json`：挂课失败原因和详情。
- `data/links/考试链接.json`：待 AI 处理的考试及失败模型配置。
- `data/links/人工考试链接.json`：需要人工处理的考试。
- `data/references/`：课程参考资料（PDF/文档）与视频 AI 导学资料。
- `data/credentials/cookies.json` / `credential_meta.json`：登录凭证（本地敏感，勿上传）。
- `data/logs/app-info.log`：DEBUG 与 INFO。
- `data/logs/app-warn.log`：WARNING。
- `data/logs/app-error.log`：ERROR 与 CRITICAL。

日志每天自动归档，文件名为 `app-info-2026-07-29.log`、`app-warn-2026-07-29.log`、`app-error-2026-07-29.log`；不跨级重复写入。

本地抓包/反推实验结果统一放在 `tools/capture/`（已 `.gitignore`，不上传）。
按 `exam_page/`、`learning/`、`documents/`、`integration/`、`terminal/`、`class_inspect/`、`topic_inspect/` 等用途分类，可重用的探针脚本保留在 `tools/` 或 `tools/capture/` 对应目录。

### 课程页探针

`tools/probe_course_page.py` 复用正式的浏览器上下文、Cookie、导航与挂课处理函数，只额外保存脱敏后的 DOM、网络、控制台和结构化失败数据：

```powershell
.\.venv\Scripts\python.exe tools\probe_course_page.py "<课程或主题 URL>"
.\.venv\Scripts\python.exe tools\probe_course_page.py "<课程 URL>" --section-index 8
.\.venv\Scripts\python.exe tools\probe_course_page.py "<课程或主题 URL>" --run-flow --flow-timeout 300
```

结果写入 `tools/capture/course_page/probe_<时间戳>/`，并更新 `latest.json`。Cookie、Authorization、access token 和常见签名参数不会写入捕获文件。整个项目强制使用可见浏览器；任何 `headless=True` 调用都会直接报错。

## 配置

```env
OPENAI_COMPLETION_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
OPENAI_COMPLETION_API_KEY=your_api_key_here
MODEL_NAME=qwen3.6-plus
AI_REQUEST_TYPE=responses
AI_ENABLE_WEB_SEARCH=0
AI_ENABLE_THINKING=0
AI_REASONING_EFFORT=medium
BROWSER_TYPE=chromium
BROWSER_CHANNEL=msedge
DEBUG_MODE=0
SUPPRESS_STARTUP_BANNER=0
```

AI 配置可选：未填写时挂课、人工考试等功能仍可用，仅 AI 自动考试需要 `OPENAI_COMPLETION_BASE_URL` / `OPENAI_COMPLETION_API_KEY` / `MODEL_NAME`。`AI_REQUEST_TYPE` 支持 `responses` 或 `chat`；Windows 下 `BROWSER_TYPE=chromium` 默认使用系统自带 Edge。
