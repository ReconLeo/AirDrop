# -*- coding: utf-8 -*-
"""AirDrop 插件页面暗色主题接入（一次性转换脚本，写回 plugin/frontend/index.html）"""
import re, io

PATH = r'H:\Coding\Projects\AirDrop\plugin\frontend\index.html'
with open(PATH, 'rb') as f:
    data = f.read().decode('utf-8')

# ---------- 1. head 三件套 ----------
# 1a) <html> 注入 data-theme-init
data = data.replace('<html lang="zh-CN">',
                    '<html lang="zh-CN" data-theme-init="{{ theme_effective }}">', 1)
# 1b) 加 theme.css 与 theme.js（在 plugin_common.js 前后）
data = data.replace('    <script src="/static/js/plugin_common.js"></script>',
                    '    <link rel="stylesheet" href="/static/css/theme.css">\n'
                    '    <script src="/static/js/plugin_common.js"></script>\n'
                    '    <script src="/static/js/theme.js"></script>', 1)

# ---------- 2. 仅 <style> 区间内做颜色变量化 ----------
def style_region_transform(body):
    # 唯一 hex 色值 → 语义变量（只在该区间出现，语义唯一）
    hex_map = {
        '#f5f7fa': 'var(--bg)',
        '#2c3e50': 'var(--text)',
        '#f8f9fa': 'var(--th-bg)',        # hover / 地址条背景（亮色值一致）
        '#ecf0f1': 'var(--border)',
        '#7f8c8d': 'var(--text-light)',
        '#3498db': 'var(--primary)',
        '#2980b9': 'var(--primary-dark)',
        '#27ae60': 'var(--success)',
        '#229954': 'var(--success-dark)',
        '#e74c3c': 'var(--danger)',
        '#95a5a6': 'var(--faint)',
    }
    for k, v in hex_map.items():
        body = body.replace(k, v)
    # white：仅 background 用 card-bg，color: white 保留（彩色按钮白字）
    body = re.sub(r'(background(?:-color)?\s*:\s*)white', r'\1var(--card-bg)', body)
    return body

start = data.index('<style>') + len('<style>')
end = data.index('</style>')
style_body = data[start:end]
new_style = style_region_transform(style_body)

# ---------- 3. </style> 前插入深色覆盖块 ----------
dark_block = (
    '\n'
    '        /* ===== 深色模式覆盖（v1.2 主题接入，复用 theme.css 语义变量） ===== */\n'
    '        :root[data-theme="dark"] body {\n'
    '            color: var(--text);\n'
    '        }\n'
    '        :root[data-theme="dark"] .upload-area.dragover {\n'
    '            background-color: #1e2b22;\n'
    '        }\n'
    '        :root[data-theme="dark"] .address-link {\n'
    '            background: var(--panel-tint);\n'
    '            color: var(--primary);\n'
    '        }\n'
    '        :root[data-theme="dark"] .address-link:hover {\n'
    '            background: var(--chip-bg);\n'
    '        }\n'
    '        :root[data-theme="dark"] .qrcode-empty {\n'
    '            background: var(--chip-bg);\n'
    '            color: var(--faint);\n'
    '        }\n'
    '        :root[data-theme="dark"] .qrcode-nav-btn {\n'
    '            background: var(--card-bg);\n'
    '            border-color: var(--border);\n'
    '            color: var(--text);\n'
    '        }\n'
    '        :root[data-theme="dark"] .qrcode-nav-btn:hover:not(:disabled) {\n'
    '            background: var(--chip-bg);\n'
    '        }\n'
)
new_style = new_style + dark_block
data = data[:start] + new_style + data[end:]

with open(PATH, 'wb') as f:
    f.write(data.encode('utf-8'))
print('转换完成。')
print('含 data-theme-init:', 'data-theme-init="{{ theme_effective }}"' in data)
print('theme.css link:', 'href="/static/css/theme.css"' in data)
print('theme.js script:', 'src="/static/js/theme.js"' in data)
print('dark 覆盖块:', '深色模式覆盖' in data)
# CRLF 校验
print('CRLF bytes:', data.count('\r'))
