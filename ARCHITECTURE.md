# 持久工作流与跨平台维护

本项目使用 Python 3.14、DBOS **3.4.0** 和本地 SQLite。DBOS 只管理执行历史，业务仓储管理任务、队列、章节证据、父子事项、考试历史和外部写入意图。单机只允许一个应用实例；不支持多机共享网络磁盘上的数据库。

## 执行入口

CLI 与 TUI 均通过 `ApplicationService` 顺序派发任务。`WorkflowHost` 在应用级事件循环线程启动 DBOS；浏览器、HTTP 和取消清理均在该循环执行。原学习、考试批次循环已删除，原公开函数仅保留薄入口。添加入口不会开始考试。

四个已注册定义为 `collect_entry_v1`、`learn_course_v1`、`run_exam_v1`、`collect_references_v1`。工作流参数和结果均为 schema version 1 的 JSON 数据。一个课程或考试的浏览器会话是一项活动；Page、Context、锁、回调、客户端均不进入检查点。资料活动复用现有已验证资源 manifest，重入不会无条件下载所有文件。

`TaskOutcome` 显式区分 COMPLETED、WAITING_SYNC、WAITING_CHILDREN、BLOCKED、NEEDS_REVIEW、FAILED 和 INTERRUPTED。DBOS SUCCESS 表示工作流返回了有效结果，不表示业务完成。评分、归档和恢复播放由 `CourseSession` 的有序维护入口处理；进度展示可并行，第二条后台点击循环已删除。

## 存储与故障窗口

`data/workflows/execution.sqlite` 完全由 DBOS 管理，应用不读写其内部表。`state.sqlite` 是业务状态的唯一来源。`core/queues/*` 是仓储投影的兼容门面，JSON 仅作首次导入和可读快照；修改快照不会自动覆盖 SQLite。

1. 业务事务先保存任务及 PLANNED 的稳定 run ID，再用同一 ID 派发 DBOS。
2. 活动先保存子事项、外部写入意图及结果，再返回检查点。结果先记为 RESULT_SAVED；引擎完成后才记 FINISHED。因此检查点前中断不会创建第二次执行。
3. 结果、任务状态和自动队列移除在同一业务事务提交。目的事项先持久化，异常、取消或批次停止不删除当前和剩余任务。
4. 进程重启时，公开 DBOSClient 在执行器启动前将旧 PENDING/ENQUEUED 执行隔离。菜单保持空闲。只有本轮选中的任务、同一有效账号及兼容版本可以恢复；每次外部活动重入仍检查账号和操作范围。
5. 已正常返回的业务阻塞，下一次操作使用新 run ID 查询。未完成执行使用旧 ID。取消完成状态和 finally 清理完成是两个时刻；Host 等待活动清理再销毁引擎。

SQLite 启用外键、WAL 和 10 秒 busy timeout。备份使用 SQLite backup API，不能直接复制运行中的主文件而忽略 WAL。清空待办会备份仓储及快照，保留考试意图和历史。不要在工作流活动内另建恢复循环。

## 身份、来源与完成证据

稳定身份包含平台、已验证账号标识、任务类型、业务 ID；课程另外保留来源类型、来源 ID 和必修标记。账号名经摘要处理，不用显示名称或 Cookie 字节作键。同一考试的详情/答题路由共用身份。未知入口保留规范 URL 的稳定摘要。

缺失来源仍为未知。完整课程路径不会被 HTML 文本扫描截短。独立课程只有在当前页面实际生成的 course-info 请求提供来源后，才复用该来源查询；不会将缺失字段直接猜成 type=1。来源观察不保存认证头。

媒体章节仅以匹配课程、章节和资源的服务器 `finishStatus == 2` 确认完成；DOM 完成文字、100% 和视频 ended 均不替代服务器证据。课程预检、逐节快跳、轮询和最后复核遵守同一 `CompletionPolicy`。服务器未知时保留待确认。H5、URL、考试及未知类型保留其专用或人工事项，多个事项独立存储及清理。

文档确认最多 60 秒，视频观看后的额外确认最多 300 秒，请求、维护和 sleep 都计入总期限，确认完成立即结束。阶段预算持久化；确认截止不会因重启重置，离线时间不会记作有效视频观看。视频缓冲补偿另外有上限。播放器状态只使用一份 JS 采样模型。

## 报名与考试

报名先查状态，允许自动报名时落盘意图后发送。响应丢失后先对账；不能确认时转人工核实，禁止重复 POST。待审核、付费、需要理由或附件属于正常 BLOCKED 结果，可以继续其他独立入口并返回菜单。

考试在答题前保存意图及可用的服务器记录基线。未确认意图按账号和考试身份共享，跨路由互认；旧的多份未知提交全部保留。恢复先查服务器，不靠关闭 retries 避免重发。自动交卷前及最终确认前再次验证意图和当前授权。课程内考试只有取得明确 exam ID 后才关联独立考试历史。原有次数阈值、答案保护、保守续考与默认人工交卷选项继续有效。

只读 API 对明确瞬态故障最多尝试三次，且有总期限。仅已核实的进度 POST 可以按只读请求重试；报名和交卷写入不作自动重试。

## 首次迁移、诊断与回退

启动时首次迁移四类链接 JSON 和 exam-history.json：校验、完整备份、单事务导入、外键检查和来源指纹。损坏文件停止迁移，源文件保留。重复执行不产生重复任务。没有可信归属的旧数据放入 `unbound`，不会在登录后自动分配给当前账号。

使用本项目 Python 运行下列工具；所有变更操作均持有单实例锁：

```text
python tools/workflow_state.py dry-run
python tools/workflow_state.py migrate
python tools/workflow_state.py status
python tools/workflow_state.py export --output <私有本地文件.json>
python tools/workflow_state.py bind-current
```

`bind-current` 是操作者明确核对归属后的操作；先更新登录凭证。遇到目标账号已有同一考试历史时事务回滚，需逐项核实，避免覆盖记录。导出含业务链接和历史，应留在私有目录，不能上传到 CI。

产生真实远端操作以后不能直接恢复旧版备份：旧备份缺少新提交意图。优先保留新库修复前进；需要降级时导出完整状态并逐项核对。

## Windows / macOS

Windows 使用 `run.bat`，macOS 使用 `run.command`（首次可 `chmod +x run.command`，或 `sh run.command`）。两者均从脚本目录启动，传递参数与退出码，支持中文和空格路径。macOS 使用 `.venv/bin/python`。

未指定浏览器时 Windows 默认 Edge，macOS 优先已安装 Chrome，再选已安装 Edge。显式设置 `BROWSER_CHANNEL=""` 使用已安装的 Playwright Chromium；运行课程时不自动下载或切换浏览器。所有业务浏览器必须可视运行。

Windows 保留 Job Object。macOS 使用 Playwright 正常关闭及有界的自有进程清理，按进程创建身份核实 PID，不按名称关闭浏览器。单实例锁、信号、路径和启动脚本各有原生测试。SIGKILL/强制结束无法执行 finally，依靠持久记录恢复。

## 回归与版本兼容

运行 `python tools/run_tests.py`。该入口先导入隔离测试配置，再发现测试，最后显式关闭 DBOS。不要从项目根直接执行默认 unittest discover：它可能先导入 UI 包，使配置早于测试隔离加载。单独测试可用 `python tests/run_suite.py tests.test_durable_workflow`。

CI 使用 `windows-2025` x64、`macos-15` arm64、`macos-15-intel` x64，Python 3.14，安装 Playwright Chromium，设置 `BROWSER_CHANNEL=""`。可视合成页面不访问真实平台。仅上传 `build/ci` 内测试日志、版本信息及合成页面失败截图；不上传 Cookie、.env、正式 data 或真实抓包。

DBOS 固定 3.4.0。升级 SDK 必须带旧历史恢复测试。改变工作流步骤顺序应保留旧定义并注册新版本，或使用框架 patch；存在旧执行时不能直接删除定义。不兼容执行保留待核实，不能静默重放。

故障回归包括派发前、远端动作后、业务提交后、迁移事务中强制终止自有子进程，以及取消清理、账号隔离、旧版本、报名响应丢失、考试别名/未确认交卷、预算和 SQLite 备份。Windows 专属 Job Object 和 POSIX 专属信号/进程测试互为补充，不整体跳过 macOS 通用测试。

Runner 标签复核依据：[GitHub runner 文档](https://docs.github.com/en/actions/how-tos/write-workflows/choose-where-workflows-run/choose-the-runner-for-a-job)。框架边界依据：[DBOS workflows](https://docs.dbos.dev/python/tutorials/workflow-tutorial)、[client](https://docs.dbos.dev/python/reference/client)、[upgrading workflows](https://docs.dbos.dev/python/tutorials/upgrading-workflows)。

## 证据范围

2026-11-02 功能与故障边界提交 `0bae9b4` 的 [三平台 CI 回执](https://github.com/jaykwok/course-afk/actions/runs/41381198422) 全部通过：每个环境运行 576 项测试，Windows 的两项 POSIX 专属测试和 macOS 的一项 Job Object 专属测试互为补充。后续提交须查看对应的 [CI 运行记录](https://github.com/jaykwok/course-afk/actions/workflows/ci.yml)，不能沿用旧提交结果。

本地 Windows 合成回归、远端 CI、真实平台只读探针分别记录。真实只读探针确认两个指定培训班已报名，一门已有课程的三个章节由服务器确认完成；没有发送新报名、学习启动或真实交卷。指定历史失败课程没有取得有效完成证据。真实平台的暂停恢复、从未完成到完成的学习同步、macOS Terminal 完整人工交互及实际媒体编码兼容性，不能用合成测试替代。

实施中发现默认测试发现顺序会早于测试隔离加载 UI：混入本地失败快照的测试条目已按原始导入指纹恢复，本轮状态库已私下隔离。测试入口已修正，不将该次运行计作正式验收。
