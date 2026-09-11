# -*- coding: utf-8 -*-
"""AirDrop v1.3.0 主题接入验证：服务端 theme_effective 注入 + 三件套"""
import sys, os, tempfile
sys.path.insert(0, os.getcwd())
os.environ.setdefault('FLASKTOOLKIT_DEV', '1')
import global_var

data = tempfile.mkdtemp(prefix='ftk_airdrop_theme_')
global_var.DATA_DIR = data
global_var.LOG_DIR = os.path.join(data, 'logs')
os.makedirs(global_var.LOG_DIR, exist_ok=True)

import app
app.load_plugins()
client = app.app.test_client()


def check(label, cookies=None, expect_theme_init=None):
    for k in ('theme',):
        client.delete_cookie(k)
    if cookies:
        client.set_cookie('theme', cookies['theme'])
    c = client.get('/plugin/airdrop')
    html = c.get_data(as_text=True)
    status = c.status_code
    has_tc = 'href="/static/css/theme.css"' in html
    has_tjs = 'src="/static/js/theme.js"' in html
    has_dti = 'data-theme-init=' in html
    # 提取实际 data-theme-init 值
    import re
    m = re.search(r'data-theme-init="([^"]+)"', html)
    dti = m.group(1) if m else None
    has_var = 'var(--bg)' in html   # 变量化样式存在
    has_dark_block = 'data-theme="dark"]' in html
    print(f"[{label}] status={status} dti={dti!r} theme.css={has_tc} theme.js={has_tjs} "
          f"varized={has_var} dark_block={has_dark_block}")
    ok = (status == 200 and has_tc and has_tjs and has_dti and dti == expect_theme_init)
    print(f"    -> {'PASS' if ok else 'FAIL'}")
    return ok


allok = True
allok &= check('默认(auto)', expect_theme_init='auto')
allok &= check('dark Cookie', {'theme': 'dark'}, expect_theme_init='dark')
allok &= check('light Cookie', {'theme': 'light'}, expect_theme_init='light')
print('ALL PASS' if allok else 'SOME FAIL')
