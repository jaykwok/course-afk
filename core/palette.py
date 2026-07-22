"""全应用统一色板。

Textual 主题(core.ui.tui_app.COURSE_THEME)与 Rich 渲染(core.ui / core.ui.tui_bridge)
共用这套颜色，保证 CLI 图标、TUI 日志、焦点高亮、按钮变体、边框、标题都用同一套
「翡翠绿主色 + 语义色」，整体色调一致，不会冷暖 / 明暗打架。

- 翡翠绿主色：品牌色。标题、边框、序号、提示语、进度、info 图标、焦点高亮。
- 语义色：成功 / 警告 / 错误。CLI 图标、TUI 日志、Textual 按钮变体共用同一只绿 / 黄 / 红。
- 中性色(白 / 灰 / 暗淡)沿用各框架默认文字色，不在此约束。
"""

# 翡翠绿主色阶（Tailwind emerald）
GREEN = "#10B981"  # 主色：焦点高亮 / 边框 / 序号 / 提示语 / 进度 / info 图标 (emerald-500)
GREEN_BRIGHT = "#6EE7B7"  # 亮绿：标题文字 / 主标题边框 / 选区 / 进度已完成段 (emerald-300)
GREEN_DEEP = "#047857"  # 深绿：Textual 次级层次 (emerald-700)

# 语义色：成功 / 警告 / 错误（CLI 图标 / TUI 日志 / 按钮变体共用）
SUCCESS = "#34D399"  # emerald-400：与主色同族，靠亮度差区分（成功仍是"更亮一档的绿"）
WARNING = "#FBBF24"
ERROR = "#F87171"
