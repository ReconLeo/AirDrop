# -*- coding: utf-8 -*-
"""AirDrop-Lite v1.0.0 冒烟测试（在 FlaskToolkit-Lite 4.2.2 runtime 上）"""
import sys, os, io, tempfile
sys.path.insert(0, os.getcwd())
os.environ.setdefault('FLASKTOOLKIT_DEV', '1')
import global_var
data = tempfile.mkdtemp(prefix='ftk_lite_airdrop_')
global_var.DATA_DIR = data
global_var.LOG_DIR = os.path.join(data, 'logs')
os.makedirs(global_var.LOG_DIR, exist_ok=True)

import app
if hasattr(app, 'load_plugins'):
    app.load_plugins()
client = app.app.test_client()


def show(label, r):
    print(f"[{label}] status={r.status_code}")


# 1. 页面
r = client.get('/plugin/airdrop')
show('GET /plugin/airdrop', r)
html = r.get_data(as_text=True)
print('   含标题:', '局域网快传' in html)
print('   无主题残留(theme.js):', 'theme.js' not in html)
print('   无主题残留(theme.css):', 'theme.css' not in html)
print('   无主题残留(data-theme):', 'data-theme' not in html)

# 2. 静态
r = client.get('/plugin-static/airdrop/qrcode.min.js')
show('GET /plugin-static/airdrop/qrcode.min.js', r)

# 3. 文件列表
r = client.get('/api/airdrop/files')
show('GET /api/airdrop/files', r)
print('   body:', r.get_data(as_text=True)[:120])

# 4. 局域网地址（Lite 无 core.network，验证 socket 回退）
r = client.get('/api/airdrop/network-addresses')
show('GET /api/airdrop/network-addresses', r)
print('   body:', r.get_data(as_text=True)[:150])

# 5. 上传（_save_uploads_sync）
r = client.post('/api/airdrop/upload',
                data={'files': (io.BytesIO(b'hello lite airdrop'), 'lite_smoke.txt')},
                content_type='multipart/form-data')
show('POST /api/airdrop/upload', r)
print('   body:', r.get_data(as_text=True)[:120])

# 6. 列表确认
r = client.get('/api/airdrop/files')
fj = r.get_json()
names = [f['name'] for f in fj] if isinstance(fj, list) else fj.get('files', [])
print('   上传后:', names)

# 7. 下载
r = client.get('/api/airdrop/download/lite_smoke.txt')
show('GET /api/airdrop/download/lite_smoke.txt', r)
print('   content:', r.get_data(as_text=True)[:80])

# 8. 删除（沙箱 os.remove 可能 500）
r = client.post('/api/airdrop/delete/lite_smoke.txt')
show('POST /api/airdrop/delete/lite_smoke.txt', r)
print('   body:', r.get_data(as_text=True)[:120])
print('SMOKE DONE')
