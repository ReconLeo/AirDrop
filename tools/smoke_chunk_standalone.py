# -*- coding: utf-8 -*-
"""独立版 AirDrop 分片断点续传冒烟测试

对运行在 BASE 的 standalone 服务做分片/续传/合并/去重/超限/小文件回归测试。
测试产生的文件统一前缀 smoke_chunk_，测试结束后自动清理（本脚本需用无审计钩子的系统 Python 运行，
否则清理阶段的 os.remove 会被灵犀沙箱审计钩子拦截而失败）。

用法：python smoke_chunk_standalone.py [BASE_URL]
"""
import sys
import os
import time
import hashlib
import json
import shutil
import requests

BASE = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:9000'
CHUNK_MB = 8
CHUNK_SIZE = CHUNK_MB * 1024 * 1024
PREFIX = 'smoke_chunk_'
# 独立版 uploads 目录（用于清理测试产物）
UPLOAD = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'standalone', 'uploads'))

passed, skipped, failed = [], [], []


def check(name, cond, detail=''):
    if cond:
        passed.append(name)
        print(f"  PASS  {name}")
    else:
        failed.append(name)
        print(f"  FAIL  {name}  {detail}")


def makedata(size):
    """构造确定性的字节数据（重复模式，便于 MD5 复算）。"""
    pattern = b'AirDrop-chunk-test-'
    return (pattern * (size // len(pattern) + 1))[:size]


def md5(data_or_path, is_path=False):
    h = hashlib.md5()
    if is_path:
        with open(data_or_path, 'rb') as f:
            for block in iter(lambda: f.read(1 << 20), b''):
                h.update(block)
    else:
        h.update(data_or_path)
    return h.hexdigest()


def post_chunk(file_id, index, total, blob):
    fd = {
        'file_id': (None, file_id),
        'chunk_index': (None, str(index)),
        'total_chunks': (None, str(total)),
        'chunk': (f'chunk-{index}', blob),
    }
    return requests.post(f'{BASE}/upload/chunk', files=fd)


def post_complete(file_id, filename, total):
    return requests.post(f'{BASE}/upload/complete', data={
        'file_id': file_id, 'filename': filename, 'total_chunks': str(total)})


def get_status(file_id):
    return requests.get(f'{BASE}/upload/status', params={'file_id': file_id})


def list_uploads():
    return requests.get(f'{BASE}/api/files').json()


def cleanup_test_files():
    """清理测试在 uploads 目录产生的 smoke_chunk_* 文件。"""
    if not os.path.isdir(UPLOAD):
        return
    removed = 0
    for fn in os.listdir(UPLOAD):
        if fn.startswith(PREFIX):
            try:
                os.remove(os.path.join(UPLOAD, fn))
                removed += 1
            except OSError as e:
                print(f"  [SKIP] 清理 {fn} 失败: {e}")
    chunks_root = os.path.join(UPLOAD, '.chunks')
    if os.path.isdir(chunks_root):
        for fid in os.listdir(chunks_root):
            if fid.startswith('smoke'):
                try:
                    shutil.rmtree(os.path.join(chunks_root, fid))
                    removed += 1
                except OSError:
                    pass
    if removed:
        print(f"  [清理] 已清理 {removed} 个测试产物")


def main():
    print(f"独立版 AirDrop 分片断点续传冒烟测试 | BASE={BASE} | CHUNK={CHUNK_MB}MB")

    # ---- 前置：确认服务可访问 ----
    r = requests.get(f'{BASE}/api/test', timeout=5)
    check('服务可访问 /api/test', r.status_code == 200 and r.json().get('status') == 'ok', r.text)

    # ---- 1) 分片上传 + 合并落盘，MD5 校验 ----
    print("[1] 分片上传 + 合并")
    fname = f"{PREFIX}big.bin"
    data = makedata(12 * 1024 * 1024)          # 12MB → 2 片（8+4）
    total = (len(data) + CHUNK_SIZE - 1) // CHUNK_SIZE
    fid = f"smoke-1-{int(time.time())}"
    for i in range(total):
        blob = data[i * CHUNK_SIZE:(i + 1) * CHUNK_SIZE]
        rc = post_chunk(fid, i, total, blob)
        check(f"  分片 #{i}/{total} 上传", rc.status_code == 200 and rc.json().get('status') == 'ok', rc.text)
    rc = post_complete(fid, fname, total)
    check("  complete 合并成功", rc.status_code == 200 and rc.json().get('status') == 'success', rc.text)
    files = {f['name']: f for f in list_uploads()}
    check(f"  文件已出现在列表: {fname}", fname in files)
    dl = requests.get(f'{BASE}/download/{fname}')
    check("  MD5 校验一致", dl.status_code == 200 and md5(dl.content) == md5(data), f"dl={dl.status_code}")

    # ---- 2) 断点续传：只传部分分片后查 status，再补缺失 ----
    print("[2] 断点续传（status 查已接收，补缺失）")
    fname2 = f"{PREFIX}resume.bin"
    data2 = makedata(16 * 1024 * 1024)         # 16MB → 2 片
    total2 = (len(data2) + CHUNK_SIZE - 1) // CHUNK_SIZE
    fid2 = f"smoke-2-{int(time.time())}"
    blob0 = data2[0:CHUNK_SIZE]
    rc = post_chunk(fid2, 0, total2, blob0)
    check(f"  首片 #{0} 上传", rc.status_code == 200, rc.text)
    st = get_status(fid2).json()
    check("  status 显示已接收 [0]", st.get('received') == [0] and st.get('total_chunks') == total2, json.dumps(st))
    blob1 = data2[CHUNK_SIZE:2 * CHUNK_SIZE]
    rc = post_chunk(fid2, 1, total2, blob1)
    check(f"  续传补片 #{1} 上传", rc.status_code == 200, rc.text)
    rc = post_complete(fid2, fname2, total2)
    check("  续传 complete 合并", rc.status_code == 200 and rc.json().get('status') == 'success', rc.text)
    dl = requests.get(f'{BASE}/download/{fname2}')
    check("  续传 MD5 校验一致", dl.status_code == 200 and md5(dl.content) == md5(data2), f"dl={dl.status_code}")

    # ---- 3) 分片不完整拒绝合并 ----
    print("[3] 分片不完整拒绝合并")
    fid3 = f"smoke-3-{int(time.time())}"
    blob = makedata(1 * 1024 * 1024)
    post_chunk(fid3, 0, 3, blob)               # total=3 只传 1 片
    rc = post_complete(fid3, f"{PREFIX}inc.bin", 3)
    check("  complete 返回 400 缺失分片", rc.status_code == 400 and '不完整' in rc.json().get('msg', ''), rc.text)

    # ---- 4) 同名去重（name_1.ext） ----
    print("[4] 同名自动加序号")
    dup_name = f"{PREFIX}dup.bin"
    dup_data = makedata(1 * 1024 * 1024)       # 1MB 单片
    total_d = 1
    fid4 = f"smoke-4-{int(time.time())}"
    rc = post_chunk(fid4, 0, total_d, dup_data)
    check("  dup 分片上传", rc.status_code == 200, rc.text)
    rc = post_complete(fid4, dup_name, total_d)
    check("  dup 首次合并", rc.status_code == 200 and '上传成功' in rc.json().get('msg', ''), rc.text)
    fid4b = f"smoke-4b-{int(time.time())}"
    post_chunk(fid4b, 0, 1, dup_data)
    rc = post_complete(fid4b, dup_name, 1)
    check("  dup 同名合并加序号 _1", rc.status_code == 200 and '_1.bin' in rc.json().get('msg', ''), rc.text)
    files = list_uploads()
    check("  _1 文件存在", any(f['name'].endswith(f'{PREFIX}dup_1.bin') for f in files))

    # ---- 5) 分片超限拒绝（>chunk_size_mb） ----
    print("[5] 分片超限拒绝")
    oversize = makedata((CHUNK_MB + 1) * 1024 * 1024)   # 9MB > 8MB
    fid5 = f"smoke-5-{int(time.time())}"
    rc = post_chunk(fid5, 0, 1, oversize)
    check("  超限分片返回 413", rc.status_code == 413 and '限制' in rc.json().get('msg', ''), f"{rc.status_code} {rc.text}")

    # ---- 6) 小文件整文件上传回归 ----
    print("[6] 小文件整传回归")
    small_name = f"{PREFIX}small.txt"
    small = b'hello standalone chunk smoke'
    rc = requests.post(f'{BASE}/upload', files={'files': (small_name, small)})
    check("  小文件整传", rc.status_code == 200 and rc.json().get('status') == 'success', rc.text)
    dl = requests.get(f'{BASE}/download/{small_name}')
    check("  小文件内容校验", dl.status_code == 200 and dl.content == small)

    # ---- 7) file_id 非法（路径穿越）拒绝 ----
    print("[7] file_id 非法拒绝")
    rc = post_chunk('../evil', 0, 1, b'x')
    check("  非法 file_id 返回 400", rc.status_code == 400, rc.text)

    # ---- 8) abort 取消清理 ----
    print("[8] abort 取消清理临时分片")
    fid8 = f"smoke-8-{int(time.time())}"
    post_chunk(fid8, 0, 2, makedata(1 * 1024 * 1024))
    rc = requests.post(f'{BASE}/upload/abort', data={'file_id': fid8})
    check("  abort 返回 ok", rc.status_code == 200 and rc.json().get('status') == 'ok', rc.text)
    st = get_status(fid8).json()
    check("  abort 后 status 无已接收分片", st.get('received') == [], json.dumps(st))

    # ---- 清理测试产物 ----
    cleanup_test_files()

    print("\n" + "=" * 50)
    print(f"结果: {len(passed)} 通过, {len(failed)} 失败, {len(skipped)} 跳过")
    if failed:
        print("失败项: " + ", ".join(failed))
        sys.exit(1)
    print("全部通过")


if __name__ == '__main__':
    main()
