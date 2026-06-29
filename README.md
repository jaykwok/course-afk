# 网上大学挂课统一入口

这是一个学习自动化工具。日常使用只需要打开统一入口，按菜单完成登录、选课、挂课、考试即可。

推荐直接使用 `run.bat`；也可以在已配置好 Python 环境后运行 `python launcher.py`。

## 怎么使用

### 1. 第一次准备

先在项目目录里准备 Python 环境：

```bash
uv venv
uv pip install -r requirements.txt
```

复制 `.env.example` 为 `.env`。**AI 参数是可选的**：不填也能用挂课、人工考试、课件收集等功能；只有「AI 自动考试」需要下面的 AI 配置（接口地址 / API Key / 模型名三项都填才生效）。没填时进入 AI 自动考试会提示未填写。

```env
OPENAI_COMPLETION_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
OPENAI_COMPLETION_API_KEY=your_api_key_here
MODEL_NAME=qwen3.6-plus
AI_REQUEST_TYPE=responses
AI_ENABLE_WEB_SEARCH=0
AI_ENABLE_THINKING=0
# AI_REASONING_EFFORT=medium
```

默认会在 Windows 上使用系统 Edge 浏览器，通常不需要额外安装浏览器。

### 2. 启动程序

双击 `run.bat`。

如果你习惯命令行，也可以运行：

```bash
uv run launcher.py
```

启动后会看到主菜单和当前状态。按菜单提示操作即可。

### 3. 更新登录凭证

第一次使用，或者状态面板提示登录凭证过期时，选择“切换账号 / 更新登录凭证”。

程序会打开浏览器，你按正常方式登录。登录成功后，程序会保存登录状态，后续一般不需要每次重新登录。

### 4. 手动选择课程 / 录入链接

如果状态面板里“课程链接”为 0，可以选择“手动选择课程 / 录入课程或考试链接”。

你可以粘贴入口链接、课程链接、考试链接、学习专区链接，或者一段包含链接的文本。普通 `Enter` 用于换行，输入完成后统一按 `Ctrl+Enter` 提交。
如果输入过程中发现内容有误，可以按 `ESC` 取消并返回主菜单。

如果检测到学习专区链接，程序会让你选择：

1. 全部学习：自动解析专区里的课程/主题链接并写入 `课程链接.json`
2. 手动选择学习模块：打开页面后你自己点击课程，程序记录新打开的真实学习链接

手动选择过程中如果打开的是考试答题页，程序会自动识别并写入 `考试链接.json`，不会混入课程链接。

普通使用建议先选择“全部学习”。如果专区页面结构特殊，自动解析不到，再用“手动选择学习模块”。

### 5. 仅挂课

有课程链接后，选择“仅挂课”或直接选择“推荐流程”。

挂课过程中：

- 当前课程完成后，会自动从 `课程链接.json` 移除
- 课程或 AI 考试队列处理完后，空的 `课程链接.json` / `考试链接.json` 会直接删除，不再询问
- 浏览器异常、课程类型不支持、无权限、链接不合规等情况会记录到 `挂课失败链接.json`
- 如果检测到考试，会写入 `考试链接.json`

程序会保留一个 `https://www.mylearning.cn/p5/index.html` 常驻主控标签页。手动关闭单个课程标签页会跳过当前课程；关闭整个浏览器窗口会退出程序。

### 6. AI 自动考试

挂课完成后，如果生成了考试链接，选择“AI 自动考试”。

如果没有检测到考试链接但仍进入“AI 自动考试”，程序会询问是否手动粘贴考试链接。支持一次输入多条链接，一行一个；按 `Enter` 换行，各平台统一使用 `Ctrl+Enter` 提交。识别到的链接会去重写入 `考试链接.json`，随后直接开始 AI 自动考试。

进入 AI 自动考试前，程序会询问是否自动交卷，默认 `N`，答题完成后由你人工确认并交卷；需要自动交卷时选择 `Y`。

AI 考试没有通过时，程序会记录当时使用的模型、请求方式、联网搜索、思考模式、推理强度。下次如果还是同一组 AI 配置，会跳过该链接并提示更换模型或人工处理，避免重复浪费考试次数。

### 7. 人工考试

以下情况会进入 `人工考试链接.json`：

- 考试剩余次数达到人工阈值
- 考试次数已超限
- AI 自动考试仍未通过
- 填空题、AI 无有效答案、考试流程异常

选择“人工考试”后，程序会逐个打开这些链接，你在浏览器中完成考试即可。

## 推荐流程

“推荐流程”会按当前状态自动决定下一步：

1. 没有登录凭证或凭证过期：先更新登录凭证
2. 没有课程链接：先手动选择课程或录入课程/考试链接
3. 有课程链接：开始挂课流程
4. 挂课生成考试链接：进入 AI 自动考试
5. AI 后仍有人工考试：提示你继续人工考试

## 输出文件

日常只需要关注这几个文件：

- `课程链接.json`：待挂课的课程/主题链接
- `挂课失败链接.json`：挂课失败或需要人工处理的课程链接，包含原因和说明
- `考试链接.json`：待 AI 自动考试的考试链接，并记录每条链接已失败的 AI 模型配置
- `人工考试链接.json`：需要人工处理的考试链接，并记录转人工原因、剩余次数和 AI 状态
- `log.txt`：完整运行日志，排查问题时使用

`课程链接.json` 示例：

```json
[
  {
    "url": "https://kc.zhixueyun.com/#/study/course/detail/..."
  }
]
```

`挂课失败链接.json` 示例：

```json
[
  {
    "url": "https://kc.zhixueyun.com/#/study/course/detail/...",
    "reason": "h5_manual_required",
    "reason_text": "H5 课程类型需要人工处理",
    "detail": {
      "source": "course_chapter"
    }
  }
]
```

常见挂课失败 `reason`：

- `no_permission`：无权限访问该学习资源
- `retryable_error`：挂课处理失败，可重新加入课程链接后再跑
- `url_type_pending`：URL 类型学习等待复查
- `h5_manual_required`：H5 课程需要人工处理
- `survey_manual_required`：调研类型需要人工处理
- `unknown_learning_type`：未知学习类型
- `other_learning_type`：非课程及考试类学习类型
- `non_compliant_url`：链接格式不符合课程/主题链接要求

`考试链接.json` 示例：

```json
[
  {
    "url": "https://kc.zhixueyun.com/#/study/course/detail/...",
    "ai_failed_model_configs": [
      {
        "model": "qwen3.6-max-preview",
        "request_type": "chat",
        "web_search": false,
        "thinking": false,
        "reasoning_effort": null
      }
    ]
  }
]
```

`人工考试链接.json` 示例：

```json
[
  {
    "url": "https://kc.zhixueyun.com/#/study/course/detail/...",
    "reason": "ai_failed",
    "reason_text": "AI 自动考试仍未通过",
    "remaining_attempts": null,
    "threshold": null,
    "ai_failed_model_configs": [
      {
        "model": "qwen3.6-max-preview",
        "request_type": "chat",
        "web_search": false,
        "thinking": false,
        "reasoning_effort": null
      }
    ]
  }
]
```

常见人工考试 `reason`：

- `attempt_threshold`：考试剩余次数已达到人工阈值
- `attempt_limit`：页面提示考试次数限制，不能再次进入考试详情页
- `ai_failed`：AI 自动考试已尝试但未通过
- `ai_no_answer`：AI 没给出有效答案
- `fill_blank`：检测到填空题
- `ai_exam_error`：AI 考试流程异常

## 浏览器行为

- 默认使用 Edge：`BROWSER_TYPE=chromium`，`BROWSER_CHANNEL=msedge`
- 打开浏览器时默认最大化
- 程序打开的 Chromium 页面会隐藏明显自动化标记，以提高短信滑块验证码的人工拖动成功率；验证码风控由平台控制，仍可能要求改用扫码或人工处理
- 挂课时会保留 mylearning 常驻主控标签页
- 关闭单个课程标签页：保留当前课程链接，继续下一条
- 关闭整个浏览器窗口（含网络异常打不开、或刚启动还没进入挂课循环时关掉）：保留剩余链接，返回主菜单，不会报错退出（挂课、AI 考试、人工考试一致）
- `Ctrl+C` 中断挂课 / 考试：保存剩余链接后返回主菜单（误触也不会丢链接，可重新选择继续）
- AI 自动考试和人工考试中关闭单个考试标签页：跳过当前考试并继续下一条

如果你改成其他浏览器，按需补安装：

- `BROWSER_TYPE=chromium` 且 `BROWSER_CHANNEL=chrome`：使用本机 Chrome，一般不需要额外安装 Playwright Chromium
- `BROWSER_TYPE=chromium` 且不设置 `BROWSER_CHANNEL`：使用 Playwright 自带 Chromium，需要执行 `playwright install chromium`
- `BROWSER_TYPE=webkit`：需要执行 `playwright install webkit`
- `BROWSER_TYPE=firefox`：需要执行 `playwright install firefox`

## 自定义参数

可选环境变量写在 `.env` 里。

### AI 参数

- `OPENAI_COMPLETION_BASE_URL`：OpenAI 兼容接口地址
- `OPENAI_COMPLETION_API_KEY`：API Key
- `MODEL_NAME`：模型名
- `AI_REQUEST_TYPE=chat|responses`：切换 `Chat Completions` 或 `Responses` 请求方式
- `AI_ENABLE_WEB_SEARCH=0|1`：是否为 AI 考试启用联网搜索；联网搜索，默认关闭
- `AI_ENABLE_THINKING=0|1`：是否开启思考模式，默认关闭
- `AI_REASONING_EFFORT=none|minimal|low|medium|high`：仅 `responses` 请求使用，优先级高于 `AI_ENABLE_THINKING`

AI 自动考试支持两种 OpenAI 兼容请求方式：

- `AI_REQUEST_TYPE=chat`：走 `Chat Completions API`
- `AI_REQUEST_TYPE=responses`：走 `Responses API`

联网搜索在两种请求方式里都会保留：

- `responses`：通过 `tools=[{"type": "web_search"}]`
- `chat`：通过 `extra_body={"enable_search": True}`

思考模式规则：

- `responses` 优先使用 `AI_REASONING_EFFORT`
- `responses` 未设置 `AI_REASONING_EFFORT` 且 `AI_ENABLE_THINKING=1` 时，传 `extra_body={"enable_thinking": True}`
- `chat` 总是显式传 `extra_body={"enable_thinking": true|false}`，避免兼容接口使用服务端默认思考模式导致正文为空

AI 自动考试跳过逻辑按整组配置匹配：同一链接如果当前模型名、`AI_REQUEST_TYPE`、`AI_ENABLE_WEB_SEARCH`、`AI_ENABLE_THINKING`、`AI_REASONING_EFFORT` 都已记录为未通过，会提示更换模型或人工考试并跳过。只要其中一项不同，例如开启联网搜索、开启思考模式、切换请求方式或调整推理强度，就会继续尝试考试。如果再次未通过，会把新的配置追加到该链接的 `ai_failed_model_configs`。

### 浏览器和日志参数

- `BROWSER_TYPE=chromium|webkit|firefox`：浏览器类型；Windows 默认使用 `chromium`
- `BROWSER_CHANNEL=msedge|chrome|空值`：浏览器通道；通常只在 `chromium` 下使用
- `DEBUG_MODE=0|1`：是否输出 DEBUG 日志
- `SUPPRESS_STARTUP_BANNER=0|1`：是否隐藏启动横幅

浏览器示例：

```env
# Windows 默认就是这组配置，可不写
BROWSER_TYPE=chromium
BROWSER_CHANNEL=msedge

# 如果以后改用 Chrome
# BROWSER_TYPE=chromium
# BROWSER_CHANNEL=chrome
```
