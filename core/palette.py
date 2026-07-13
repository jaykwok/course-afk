"""全应用统一色板。

Textual 主题(core.tui_app.COURSE_THEME)与 Rich 渲染(core.ui / core.tui_bridge)
共用这套颜色，保证 CLI 图标、TUI 日志、焦点高亮、按钮变体、边框、标题都用同一套
「青色主色 + 语义色」，整体色调一致，不会冷暖 / 明暗打架。

- 青色主色：品牌色。标题、边框、序号、提示语、进度、info 图标、焦点高亮。
- 语义色：成功 / 警告 / 错误。CLI 图标、TUI 日志、Textual 按钮变体共用同一只绿 / 黄 / 红。
- 中性色(白 / 灰 / 暗淡)沿用各框架默认文字色，不在此约束。
"""

# 青色主色阶（Tailwind cyan）
CYAN = "#06B6D4"  # 主色：焦点高亮 / 边框 / 序号 / 提示语 / 进度 / info 图标
CYAN_BRIGHT = "#22D3EE"  # 亮青：标题文字 / 主标题边框 / 选区
CYAN_DEEP = "#0E7490"  # 深青：Textual 次级层次

# 语义色：成功 / 警告 / 错误（CLI 图标 / TUI 日志 / 按钮变体共用）
SUCCESS = "#34D399"
WARNING = "#FBBF24"
ERROR = "#F87171"
