# 网上大学课程自动化工具

统一处理登录、课程链接收集、挂课、AI 考试、人工考试和课程资料保存。主菜单显示当前账号、账号有效期、队列数量和建议操作。

## 安装与启动

```bash
uv venv
uv pip install -r requirements.txt
```

复制 `.env.example` 为 `.env`，双击 `run.bat`，或运行：

```bash
python launcher.py
```

## 输出文件与状态

- `课程链接.json`：待处理的课程和主题。
- `挂课失败链接.json`：挂课失败原因和详情。
- `考试链接.json`：待 AI 处理的考试及失败模型配置。
- `人工考试链接.json`：需要人工处理的考试。
- `参考资料/`：课程课件（PDF/文档）与视频 AI 导学资料。
- `log.txt`：运行日志。

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
