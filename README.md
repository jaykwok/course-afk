# 网上大学课程自动化工具

统一处理登录、课程链接收集、挂课、AI 考试、人工考试和课程资料保存。主菜单显示当前账号、账号有效期、队列数量和建议操作。

课程链接收集支持知学云 `topic` 学习专区、培训班，以及天翼专家助手
`/expert-assist-web/casePool` 案例库集合页。普通 `topic` 会先使用已加载的 Cookie
直接请求 HTML 并提取课程，只有直取失败或页面没有返回链接时才进入页面并点击
“更多”。案例库认证后会直接调用列表接口批量取得记录 URL；Vue 卡片读取与自动
翻页仅作为接口异常时的兜底。该站使用独立的 SSO token，直接粘贴无 `code` 且尚未登录的地址时，
程序会自动点击登录入口，利用已加载的 Cookie 跟随认证跳转，等待页面回到案例库
后再开始提取；认证未完成时会明确报错，不会误报为空页面。

课程详情中的内嵌考试也采用接口优先：从课程详情
`courseChapterSections[].sectionType=9` 读取考试 `resourceId`，再经考试基本信息和
用户记录接口判断通过状态及剩余次数。已通过/待评卷考试跳过，剩余次数达到安全
阈值时转人工，其余写入直达试卷队列；接口异常时继续使用原页面状态与按钮文案兜底。

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

### 可选：PDF 转 Markdown（PP-OCRv6）

菜单 7 下载完课程资料后，仅在检测到 PDF 时询问是否转换。选择“是”会使用
PP-StructureV3 进行版面分析、PP-OCRv6_medium 进行文字识别，每个 PDF 输出一个
适合 LLM/RAG 阅读的 Markdown；PPTX、DOCX、XLSX 等不会进入 OCR 流程。
成功转换后会删除对应 PDF，并把课程 Markdown 与 `文档索引.md` 中的 PDF 链接
替换为 `.md` 链接；转换失败的 PDF 会保留，避免资料丢失。

该功能依赖 PaddlePaddle，目前请使用 Python 3.9-3.13。OCR 依赖是可选的，不放入
基础 `requirements.txt`。Windows PowerShell 一键安装：

```powershell
# 自动检测 NVIDIA GPU；无 GPU 时安装 CPU 版
.\tools\setup_ocr.ps1

# 也可明确指定
.\tools\setup_ocr.ps1 -Backend Cpu
.\tools\setup_ocr.ps1 -Backend Gpu -Cuda cu130

# 只检测官方是否提供该版本，不执行安装
.\tools\setup_ocr.ps1 -Backend Gpu -Cuda cu130 -CheckOnly
```

脚本会查询 PaddlePaddle 官方软件源，报告当前 Windows/Python 环境最新可用的 CUDA
wheel，并自动选择不高于本机驱动能力的最新版本。明确指定 `-Cuda cuXYZ` 时也会先
验证；切换 CUDA 版本或 CPU/GPU 后端时会替换原后端。随后脚本安装
适合机器的 PaddlePaddle 3.3.0 后端，再安装
`requirements-ocr.txt` 中的 PaddleOCR 3.7 文档解析组件。可通过
`COURSE_AFK_OCR_DEVICE=cpu` 或 `gpu:0` 覆盖运行设备；默认自动选择。

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
