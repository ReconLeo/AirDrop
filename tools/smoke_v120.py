# -*- coding: utf-8 -*-
"""airdrop v1.2.0 E2 上传冒烟（save_uploads 迁移验证）——隔离环境，上传目录指向临时目录。

验证：正常单/多文件上传落盘、重名去重加序号、路径穿越净化无越界、中文文件名保留、
下载内容一致、删除。运行需要 FlaskToolkit 框架代码（FLASKTOOLKIT_ROOT）。
"""
import os, sys, io, tempfile, shutil

_PROJECT_ROOT = os.environ.get('FLASKTOOLKIT_ROOT',
                               os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _PROJECT_ROOT)

import global_var

results = []
def check(name, cond, detail=''):
    results.append((name, cond, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")

def build_env():
    root = tempfile.mkdtemp(prefix='ftk_airdrop_e2_')
    up = os.path.join(root, 'uploads')
    os.makedirs(os.path.join(root, 'plugins'))
    os.makedirs(os.path.join(root, 'temp'))
    os.makedirs(up)
    for fn in ('__init__.py', 'base_plugin.py', 'airdrop.py'):
        shutil.copy(os.path.join(_PROJECT_ROOT, 'plugins', fn),
                    os.path.join(root, 'plugins', fn))
    for _m in [m for m in list(sys.modules) if m == 'plugins' or m.startswith('plugins.')]:
        del sys.modules[_m]
    sys.path.insert(0, root)
    saved = {}
    for attr, val in (('BASE_DIR', root), ('UPLOAD_TEMP_DIR', os.path.join(root, 'temp')),
                      ('STATS_FILE', os.path.join(root, 'data', 'stats.json')),
                      ('PLUGIN_CACHE_DIR', os.path.join(root, '.plugin_cache')),
                      ('PLUGIN_CACHE_FILE', os.path.join(root, '.plugin_cache', 'plugin_discovery_cache.json'))):
        saved[attr] = getattr(global_var, attr, None)
        setattr(global_var, attr, val)
    return root, saved, up

def restore_env(saved, root):
    for attr, val in saved.items():
        if val is None:
            try: delattr(global_var, attr)
            except AttributeError: pass
        else:
            setattr(global_var, attr, val)
    shutil.rmtree(root, ignore_errors=True)

def main():
    root, saved, up = build_env()
    try:
        import app as appmod
        from core.plugin_loader import load_plugins
        app = appmod.app
        app.config["TESTING"] = True
        from jinja2 import ChoiceLoader, FileSystemLoader
        app.jinja_env.loader = ChoiceLoader([
            FileSystemLoader(os.path.join(root, 'templates')),
            FileSystemLoader(os.path.join(_PROJECT_ROOT, 'templates')),
        ])
        load_plugins()
        ad = global_var.plugins.get('airdrop')
        # 上传目录重定向到隔离目录（save_uploads dest_dir 用绝对路径）
        ad.upload_folder = up
        ad.upload_folder_abs = up
        client = app.test_client()

        def upload(**files):
            return client.post('/api/airdrop/upload',
                               data={'files': [(io.BytesIO(c), n) for n, c in files.values()]},
                               content_type='multipart/form-data')

        # 1. 单文件正常上传
        r = upload(hello=('hello.txt', b'hello v1.2.0'))
        check("单文件上传", r.status_code == 200 and os.path.exists(os.path.join(up, 'hello.txt')),
              f"status={r.status_code} body={r.get_data(as_text=True)[:80]}")

        # 2. 多文件上传
        r = upload(a=('a.txt', b'aaa'), b=('b.txt', b'bbb'))
        check("多文件上传", r.status_code == 200 and os.path.exists(os.path.join(up, 'a.txt'))
              and os.path.exists(os.path.join(up, 'b.txt')), f"status={r.status_code}")

        # 3. 重名去重加序号
        r = upload(hello2=('hello.txt', b'hello dup'))
        check("重名去重加序号", os.path.exists(os.path.join(up, 'hello_1.txt')),
              f"files={sorted(os.listdir(up))}")

        # 4. 中文文件名保留
        r = upload(zh=('中文文件.txt', '中文内容'.encode('utf-8')))
        check("中文文件名", os.path.exists(os.path.join(up, '中文文件.txt')), f"files={sorted(os.listdir(up))}")

        # 5. 路径穿越净化（../../evil.txt → evil.txt，不越界）
        evil_outside = os.path.join(root, '..', '..', 'evil_e2_outside.txt')
        r = upload(evil=('../../evil.txt', b'evil'))
        check("路径穿越净化落盘", os.path.exists(os.path.join(up, 'evil.txt'))
              and not os.path.exists(os.path.join(up, '..', '..', 'evil.txt')),
              f"files={sorted(os.listdir(up))}")
        check("未越界到目录外", not os.path.exists(evil_outside), '')

        # 6. 列表含全部
        r = client.get('/api/airdrop/files')
        data = r.get_json()
        name_set = {f['name'] for f in data} if isinstance(data, list) else {f['name'] for f in data.get('files', [])}
        check("文件列表完整", {'hello.txt', 'hello_1.txt', 'a.txt', 'b.txt', '中文文件.txt', 'evil.txt'} <= name_set,
              f"names={sorted(name_set)}")

        # 7. 下载内容一致
        r = client.get('/api/airdrop/download/中文文件.txt')
        check("中文文件下载内容", r.status_code == 200 and r.get_data() == '中文内容'.encode('utf-8'),
              f"status={r.status_code} len={len(r.get_data())}")

        # 8. 删除
        r = client.post('/api/airdrop/delete/a.txt')
        check("删除文件", r.status_code == 200 and not os.path.exists(os.path.join(up, 'a.txt')),
              f"status={r.status_code}")

        print(f"\n==== AirDrop v1.2.0 E2 上传冒烟：共 {len(results)} 项，通过 "
              f"{sum(1 for _, c, _ in results if c)}，失败 {sum(1 for _, c, _ in results if not c)} ====")
        return all(c for _, c, _ in results)
    finally:
        restore_env(saved, root)

if __name__ == '__main__':
    sys.exit(0 if main() else 1)
