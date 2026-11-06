# 网上大学自动化挂课工作台

面向中国电信网上大学的课程学习、AI 辅助答题和课件下载工具。使用 TypeScript、Node.js 与 [nodriver-ts](https://github.com/jaykwok/nodriver-ts)，启动后打开 Web 工作台。

## 安装与启动

安装 **Node.js 24.22.0 或更高版本**，以及 Microsoft Edge 或 Google Chrome。

```sh
npm ci
npm run build
node dist/src/main.js
```

Windows 默认查找 Edge，其次 Chrome；macOS 默认查找 Chrome，其次 Edge。

工作台默认监听 `http://127.0.0.1:10000`，启动后自动打开。

浏览器以可见窗口运行，使用项目独立的 `data/browser-profile/`，后续启动继续复用。这个本地个人资料关闭浏览器账号自动登录、同步和扩展，不使用日常浏览器的个人资料。

## 配置

点击工作台右上角的 **设置**，填写并保存。无需复制配置模板或编辑环境文件。

- **AI**：选择 Responses 或 Chat Completions，填写接口地址、API Key 和模型名称。默认接口地址为 `https://api.openai.com/v1`；可以填写提供相同协议的接口。
- **高级设置**：调整答案输出格式、请求超时、重试次数、学习进度检查间隔和章节期限。
- **浏览器**：留空自动查找，也可以填写可执行文件的完整路径，重启后生效。

设置统一保存在 `data/settings.json`，下次启动自动读取。API Key 不会回显到页面；留空保存会保留已有密钥，也可勾选清除。AI 与学习设置在下一轮任务中生效。未配置 AI 时仍可学习课程和下载资料。

## 使用

1. 点击 **登录 / 更新凭证**，在平台页面完成登录；工作台会核验账号并保存登录凭证。
2. 粘贴课程、主题、培训班、专区、案例库或考试链接，点击 **解析并加入队列**。系统递归展开入口、去重并保留来源关系；部分入口失败时已发现的任务仍会保留。
3. 点击 **开始学习**，按队列处理课程。完成状态以平台返回的章节进度为准。
4. 点击 **开始考试**，使用已配置的模型辅助答题。**答题后自动交卷默认关闭**；题目不完整、剩余次数不足或结果不确定时保留试卷供人工复查。
5. 点击 **下载课件与导学**，保存课程或主题内的 PDF、Word、PowerPoint 课件以及导学内容和资料索引。

也可点击 **在平台选择课程**，在打开的入口页中点击所需资源，工作台记录课程或考试链接，选完后回到工作台确认。

任务运行时可取消本轮，学习任务还支持 **本课后停止**。任务结束后立即恢复工作台操作，下一步建议也可直接点击。培训班需要报名时会先询问；无法自动报名的入口保留待处理。

一个应用实例复用一个浏览器与 context，管理页与业务标签共享登录环境。切换账号或更新凭证会清理平台会话并重新核验，管理页保持登录。关闭服务时收尾当前工作，结束后关闭应用拥有的浏览器。

## 文件位置

| 路径                    | 内容                                     |
| ----------------------- | ---------------------------------------- |
| `data/settings.json`    | AI、浏览器和学习设置                     |
| `data/credentials.json` | 平台登录凭证                             |
| `data/browser-profile/` | 项目专用浏览器个人资料                   |
| `data/workbench.sqlite` | 各账号任务、队列、运行记录与考试复查状态 |
| `data/references/`      | 按账号与课程保存的课件、导学及索引       |
| `data/backups/`         | 清空队列前的数据库备份                   |
| `data/exports/`         | 从工作台导出的队列明细                   |
| `logs/`                 | 运行日志                                 |

`data/` 含有凭证和 API Key，请仅在可信设备保存，不要上传到公共仓库。运行数据与配置均已从 Git 中排除。

## 开发与检查

```sh
npm run check
npm run build
npm test
npm run check-startup
```

默认测试使用模拟协议和 HTTP 服务，不启动浏览器。真实浏览器测试需要显式开启，整轮复用一个可见浏览器；独立测试配置保存在 `build/browser-tests/browser-profile/`，后续测试继续复用，并在启动前检查密码检测缓存：

```powershell
$env:COURSE_AFK_BROWSER_TEST='1'
npm run build
npm run test:browser
```

```sh
COURSE_AFK_BROWSER_TEST=1 npm run test:browser
```

关闭工作台后，可用 `npm run state` 查看数据数量，或用 `npm run state -- --output build/state.json` 导出任务记录。部署使用 `npm ci` 安装锁定依赖，`npm run build` 构建后运行 `node dist/src/main.js`。
