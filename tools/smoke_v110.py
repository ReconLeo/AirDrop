# -*- coding: utf-8 -*-
"""airdrop-v1.1.0 插件包安装后加载冒烟测试（Phase C）"""
import sys, os, io, tempfile
sys.path.insert(0, os.getcwd())
os.environ.setdefault('FLASKTOOLKIT_DEV', '1')
import global_var

# 隔离统计/日志等数据目录（插件 upload_folder 仍按配置 plugins/configs/airdrop.json 的相对 'uploads'）
data = tempfile.mkdtemp(prefix='ftk_airdrop_verify_')
global_var.DATA_DIR = data
global_var.LOG_DIR = os.path.join(data, 'logs')
os.makedirs(global_var.LOG_DIR, exist_ok=True)

# 直接 import app 模块（模块级 app 实例）；load_plugins() 在 __main__ 内执行，此处需显式调用
import app
app.load_plugins()
client = app.app.test_client()


def show(label, r):
    print(f"[{label}] status={r.status_code} type={r.mimetype} len={len(r.get_data())}")


# 1. 主页面
r = client.get('/plugin/airdrop')
show('GET /plugin/airdrop', r)
html = r.get_data(as_text=True)
print('   页面含 AirDrop 标识:', 'AirDrop' in html)

# 2. 静态资源
r = client.get('/plugin-static/airdrop/qrcode.min.js')
show('GET /plugin-static/airdrop/qrcode.min.js', r)

# 3. 文件列表 API
r = client.get('/api/airdrop/files')
show('GET /api/airdrop/files', r)
print('   body:', r.get_data(as_text=True)[:200])

# 4. 网络地址
r = client.get('/api/airdrop/network-addresses')
show('GET /api/airdrop/network-addresses', r)
print('   body:', r.get_data(as_text=True)[:150])

# 5. 上传一个文件
r = client.post('/api/airdrop/upload',
                data={'files': (io.BytesIO(b'hello airdrop v1.1.0 pack'), 'smoke_v110.txt')},
                content_type='multipart/form-data')
show('POST /api/airdrop/upload', r)
print('   body:', r.get_data(as_text=True)[:150])

# 6. 列表确认出现
r = client.get('/api/airdrop/files')
files_json = r.get_json()
names = [f['name'] for f in files_json] if isinstance(files_json, list) else files_json.get('files', [])
print('   上传后文件列表:', names)

# 7. 下载
r = client.get('/api/airdrop/download/smoke_v110.txt')
show('GET /api/airdrop/download/smoke_v110.txt', r)
print('   content:', r.get_data(as_text=True)[:80])

# 8. 删除
r = client.post('/api/airdrop/delete/smoke_v110.txt')
show('POST /api/airdrop/delete/smoke_v110.txt', r)
print('   body:', r.get_data(as_text=True)[:120])
print('SMOKE DONE')
