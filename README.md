# 网上大学课程自动化工具

处理登录、课程链接收集、挂课、AI 辅助答题、人工审查交卷和课程参考资料保存。正式入口为 Textual 界面，主菜单显示账号、凭证有效期及待办数量。

## 安装与启动

项目最低要求 64 位 Python 3.14。`requirements.txt` 不固定依赖版本，安装时选择当前环境可用的最新兼容版本。

```powershell
uv venv --python 3.14
uv pip install --upgrade -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe launcher.py
```

也可以双击 `run.bat`，或在已激活虚拟环境中运行 `python launcher.py`。Windows 默认使用系统 Edge，通常无需额外下载 Chromium。使用 Playwright 自带浏览器时，在 `.env` 设置 `BROWSER_CHANNEL=""`，并执行 `python -m playwright install chromium`。Linux/macOS 需安装相应浏览器；跨平台代码路径保留，但本轮未在这些系统执行集成测试。

一级菜单直接提供开始挂课、AI 考试、添加课程 / 考试链接和课程资料；任务管理与账号登录使用二级菜单；`0` 退出，二级菜单 `0` 返回。常规窗口显示功能说明，窄窗口只显示简短标题。任务管理可查看统计、重试失败课程、查看链接，以及清空课程与失败/复查记录，或清空全部待办（含考试与复查）。清空前展示数量并确认，原文件备份到 `data/backups/clear-links-时间/`；登录凭证、考试提交历史和已保存资料不在清空范围内。

「账号与登录」二级菜单提供更换账号、重新登录/刷新凭据、查看登录状态和清空本地登录凭据。清空操作默认取消，确认后移除 Cookie 与登录元数据，保留待办和资料；仍有待办时只保留非敏感的账号归属，重新登录继续核对归属，防止换账号后处理错账号的任务。

## 工作流与完成判据

课程收集支持 `topic` 学习专区、主题详情、培训班和天翼专家助手 `/expert-assist-web/casePool`。普通 `topic` 的 HTTP 首屏只提供种子链接，随后仍通过浏览器展开“更多”。案例库经独立 SSO 认证后遍历全分类及分页；接口失败时的页面兜底标记为不完整，并保留入口供重试。未知章节类型保留主题待办。

添加培训班时先检查报名状态：已报名直接收集，未报名则先报名，重新确认报名成功后才获取课程链接。报名失败、待审核、待缴费或需要填写理由/提交附件时，保留培训班入口及具体原因，完成平台要求后可重新添加；不会仅因能读取活动列表就把无学习权限的课程加入队列。

学习只有取得完成标记才移出待办。文档同步超时会保留失败/复查记录；WAF 拦截会停止本轮挂课。挂课与考试独立启动，挂课结束只提示考试待办，不自动进入考试。

文档与视频共用限时同步确认流程：文档最多 60 秒，视频观看结束后额外最多 300 秒，查询和页面操作耗时计入上限。进入确认阶段立即只读查询服务器，此后文档约每 10 秒、视频约每 30 秒复查，服务器确认完成即结束本节；接口暂不可用时参考页面明确完成标记，服务器仍未完成时不以页面文字覆盖。某节超时后继续学习其他章节，整课结束时还会复核该节，并记录未确认章节的页面文字及服务器状态。

挂课和课程内考试共用课程页准备步骤：进入后先检查评分弹窗，有则处理，无则继续，再准备课程学习视图和目标章节。浏览器证书验证失败时停止本轮操作、保留未确认待办并返回菜单；需修复运行电脑的证书信任或网络环境后重试。

专题课程保存为 `course/detail/课程ID/6/required/专题ID`，保留来源与必修标记；同一课程属于不同专题时分别保留。普通独立课程仍支持裸 UUID 链接。完整的 `type=6&required=0或1&sourceId=专题ID` 查询参数输入会转为上述路径；不完整的来源参数不降级为裸链接。手动选课时会将平台 `detailInfo…` 临时入口解析为可持久保存的链接；无法解析则保留入口供重试。历史裸链接无法反推出来源，需从原专题重新收集，或使用明确的来源映射迁移。

考试只有统一的 AI 答题入口。每轮启动询问是否自动交卷，默认 **N（不自动交卷）**：AI 作答后保持原页面打开，供人工审查、补答并手动提交；关闭页面会保留待办。显式开启后，仅满足题目完整、答案合法、选项读回一致等条件的考试自动交卷。低次数 / 阈值内、次数未知、未确认提交的续答、既往同配置失败、已有答案待核对、单题导航或作答不完整等情况仅关闭当前考试的自动交卷，后续正常考试仍按本轮开启设置执行。媒体内容、未知题型、填空、拒答、截断、断流或非法答案会留下具体题号和原因，供人工在当前页补答，不从解释文字或 reasoning 中猜答案。

剩余 1 次、达到次数阈值或次数未知的考试也使用同一 AI 答题流程。旧人工队列会在启动考试时恢复为 AI 补答或结果复查，并保留“仅人工交卷”标记。发现上次未确认的考试时，只续答当前试卷或“继续考试”入口，保留已有答案、补答空题，不自动重开新一轮。已通过的考试跳过并清除旧复查待办；平台禁止进入或待评卷的考试仍保留复查。关闭页面不能作为交卷成功的证据。

考试开始答题前先保存提交意图，避免强制退出后盲目重考。存在未确认意图时，下次先核对结果，不能确认则转人工。这个判据偏保守：取消可能发生在真正提交之前，仍需人工核对。同账号、同模型配置的失败历史跨队列保留；配置指纹包含接口、协议、能力开关与提示词版本，不含密钥。

| 操作 | 行为 |
|---|---|
| 操作中的 ESC / Ctrl+C | 请求取消当前流程，保留未确认待办并清理资源；重复取消不打断收尾 |
| 主菜单退出或 Ctrl+C | 停止工作线程后退出，错误以非零退出码报告 |
| 关闭课程/考试页 | 保留未验证任务，不作为完成证据 |
| 学习时关闭心跳页 | 当前课程结束后停止，不开下一门课或考试阶段 |
| Windows 强制结束 Python | Job Object 随进程退出回收自有浏览器进程树；依靠之前的检查点恢复 |
| POSIX SIGTERM / SIGKILL | SIGTERM 转为应用退出；SIGKILL 无法捕获，不能保证 finally 或最后一次保存 |

Windows 使用 Job Object 管理自有 Playwright driver 与浏览器进程树，并等待整个 Job 终止。应用关闭给工作线程 25 秒收尾，超时以失败状态结束进程；这不等于保证未完成的远端操作已被撤销。

## AI 与配置

AI 是可选功能；未配置时仍可登录、挂课和收集资料。以下为兼容服务示例：

```env
OPENAI_COMPLETION_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
OPENAI_COMPLETION_API_KEY=your_api_key_here
MODEL_NAME=qwen3.6-plus
AI_PROVIDER=compatible
AI_REQUEST_TYPE=responses
AI_OUTPUT_MODE=prompt_json
AI_ENABLE_WEB_SEARCH=0
AI_ENABLE_THINKING=0
AI_REQUEST_TIMEOUT=60
AI_TOTAL_TIMEOUT=180
AI_MAX_RETRIES=2
```

将占位密钥替换为自己的配置。标准 OpenAI 使用 `AI_PROVIDER=openai`；协议可选 `chat` / `responses`。`AI_OUTPUT_MODE=json_schema` 要求接口支持严格结构化输出；默认 `prompt_json` 同样在本地严格验证。推理强度可按模型能力设置，例如 `AI_REASONING_EFFORT=medium`；搜索、温度和思考参数的组合见 [.env.example](.env.example)。不会把兼容服务的私有参数无条件发给标准 OpenAI。

每次 AI 考试批次重新读取 `.env`；运行中的批次保持同一配置。环境变量优先。浏览器、节奏、日志和数据目录的文件配置在启动时读取，修改后重启。非法 AI 配置只阻止 AI 操作，不应阻断其他菜单。

本项目采用固定工作流和受约束模型回答，不提供任意本地工具给模型。题干、选项和搜索结果按数据处理，不能改变系统规则。模型仅返回版本化 JSON 答案协议；结构合法不代表事实一定正确。本轮测试没有测量模型事实准确率。

## 课程资料

主菜单「课程资料」接收知学云**主题详情**链接，保存课程 PDF、Word、PowerPoint 与视频 AI 导学文字，不下载视频。下载逐跳校验 HTTPS 主机、大小和文件格式，Authorization 仅发往知学云 API 主机。资料按账号与主题集合确定输出目录，`manifest.json` 记录每项结果，取消后可用同一输入重试；完整且哈希匹配的文件可以复用。

这些资料是离线导出产物，**没有自动进入答题提示词，也没有接入 RAG**。

## 数据、诊断与兼容性

默认数据根目录为项目下 `data/`，可用 `COURSE_AFK_DATA_DIR` 指向独立目录。同一目录仅允许一个正式应用实例，队列读改写串行化，文件采用原子替换。

| 路径 | 内容 |
|---|---|
| `data/links/课程链接.json` | 待学习课程和主题 |
| `data/links/挂课失败链接.json` | 失败、人工分流、入口不完整与 URL 复查记录 |
| `data/links/考试链接.json` | AI 待办 |
| `data/links/人工考试链接.json` | 考试答案及评卷结果复查（兼容旧文件名） |
| `data/links/exam-history.json` | 按账号隔离的提交意图及失败配置历史 |
| `data/credentials/cookies.json` | version 1 凭证包：Cookie 与账号元数据一起提交 |
| `data/references/` | 资料原件、AI 导学文字与下载清单 |
| `data/logs/app-info.log` | DEBUG / INFO 日志 |
| `data/logs/app-warn.log` | WARNING 日志 |
| `data/logs/app-error.log` | ERROR / CRITICAL 与异常堆栈 |

登录必须取得账号信息和 Cookie 才替换凭证；失败保留原文件。还有旧账号待办时拒绝切换到另一稳定账号；只有显示名可用时要求确认待办归属。旧 Cookie 数组与 `credential_meta.json` 只读兼容，下次成功登录写单一凭证包。更早的根目录平铺数据不自动迁移。

日志在零点后的下一次写入时轮转；无日期文件只代表当前日志，各级互不重复。默认不记录完整题干/答案。探针输出保留 HTML 结构并脱敏常见凭证、签名 URL，仍需检查后再分享；账号、课程名称和本地路径可能属于私人信息。

```powershell
.\.venv\Scripts\python.exe tools\probe_course_page.py "<课程或主题 URL>"
.\.venv\Scripts\python.exe tools\probe_course_page.py "<课程 URL>" --section-index 8
.\.venv\Scripts\python.exe tools\probe_course_page.py "<课程 URL>" --run-flow --flow-timeout 300
```

探针结果位于被 Git 忽略的 `tools/capture/`。`--run-flow` 会执行真实学习流程，应只在准备操作该课程时使用。业务源码按 `auth`、`browser`、`learning`、`exam`、`discovery`、`queues`、`app`、`ui` 分包；`runtime.py`、`storage.py`、`diagnostics.py` 统一生命周期、持久化和脱敏。

内部同步登录/浏览器辅助 API 已移除，统一使用异步接口；CLI 渲染接口保留为 TUI 桥接边界。升级时请同步更新调用方。Playwright 版本与浏览器启动参数、driver 所有权适配器关联，升级后需重跑生命周期与浏览器验证。

## 验证

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -t . -v
.\.venv\Scripts\python.exe -m compileall -q core tools tests
```

测试使用临时数据目录、模拟页面/服务及自建短命子进程，不读取正式凭证，也不操作真实考试。真实平台 DOM、评分单位和第三方模型能力仍需在对应环境验证。详见审计报告的验收记录。
