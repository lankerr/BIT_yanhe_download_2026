"""
极客黑主题色板（GitHub Dark / DaisyUI dark 风格）。

gui_app.py 所有界面配色统一从这里取。注释里的原始色板：
背景 #0d1117，面板 #161b22，文字 #c9d1d9，强调 #58a6ff，
成功 #3fb950，警告 #f2cc60，错误 #f85149
"""

# ---- 常量色 ----
G_BG = "#0d1117"          # 窗口背景
G_PANEL = "#161b22"       # 面板背景
G_PANEL_HI = "#21262d"    # 面板高亮（悬浮按钮等）
G_BORDER = "#30363d"      # 边框
G_TEXT = "#c9d1d9"        # 主文字
G_TEXT_DIM = "#8b949e"    # 次要文字
G_ACCENT = "#58a6ff"      # 强调色（蓝）
G_ACCENT_H = "#79c0ff"    # 强调色悬浮
G_SUCCESS = "#3fb950"     # 成功绿
G_WARN = "#f2cc60"        # 警告黄
G_ERROR = "#f85149"       # 错误红

# ---- 字典形式（供 THEME["xxx"] 访问）----
THEME = {
    "bg": G_BG,
    "panel": G_PANEL,
    "panel_hi": G_PANEL_HI,
    "border": G_BORDER,
    "text": G_TEXT,
    "text_dim": G_TEXT_DIM,
    "accent": G_ACCENT,
    "accent_h": G_ACCENT_H,
    "success": G_SUCCESS,
    "warn": G_WARN,
    "warn_h": "#e3b341",   # 警告悬浮（深一档的黄）
    "error": G_ERROR,
    "error_h": "#da3633",  # 错误悬浮（深一档的红）
    "code_bg": "#010409",  # Token 输入区代码底色
    "code_text": "#7ee787",  # 代码文字（终端绿）
}
