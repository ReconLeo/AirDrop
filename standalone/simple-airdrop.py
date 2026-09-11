from flask import Flask, render_template, request, send_from_directory, jsonify, Response
from urllib.parse import quote
import os
import re
import time
import zipfile
import json
import logging
import socket
import subprocess
import platform
from logging.handlers import RotatingFileHandler
from io import BytesIO

app = Flask(__name__)

# ============================================================
# 1. 配置文件管理 (config.json)
# ============================================================
CONFIG_FILE = 'config.json'

DEFAULT_CONFIG = {
    "log": {
        "enabled": True,
        "max_file_size_mb": 10,
        "level": "INFO"
    },
    "server": {
        "port": 5000,
        "max_content_length_gb": 1,
        "upload_folder": "uploads",
        "expire_seconds": 86400,
        "JSON_AS_ASCII": False,
        "debuggable": True
    }
}

LOG_LEVEL_MAP = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL
}


def load_config():
    """加载配置文件，不存在则自动创建并提示"""
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=4)
        print(f"[配置] 配置文件 '{CONFIG_FILE}' 不存在，已自动创建。")
        print(f"[配置] 请手动修改 '{CONFIG_FILE}' 中的参数后重启服务。")
        return dict(DEFAULT_CONFIG)

    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)

        # 深度合并：确保旧配置文件中新增了默认配置项
        changed = False

        def deep_merge(default, current, path=""):
            nonlocal changed
            for key, default_val in default.items():
                full_key = f"{path}.{key}" if path else key
                if key not in current:
                    current[key] = default_val
                    changed = True
                    print(f"[配置] 新增配置项 '{full_key}': {default_val}")
                elif isinstance(default_val, dict) and isinstance(current[key], dict):
                    deep_merge(default_val, current[key], full_key)

        deep_merge(DEFAULT_CONFIG, config)

        # 如果配置有更新，写回文件
        if changed:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=4)
            print(f"[配置] 配置文件 '{CONFIG_FILE}' 已自动更新，新增了缺失的配置项。")

        return config
    except (json.JSONDecodeError, Exception) as e:
        print(f"[配置] 配置文件解析失败 ({e})，使用默认配置。")
        return dict(DEFAULT_CONFIG)


config = load_config()

# ============================================================
# 应用配置到 Flask
# ============================================================
server_config = config.get('server', DEFAULT_CONFIG['server'])
 
# 端口
PORT = server_config.get('port', 5000)
 
# 上传文件存储目录（支持绝对路径和相对路径）
upload_folder = server_config.get('upload_folder', 'uploads')
app.config['UPLOAD_FOLDER'] = upload_folder
 
# 文件有效期
app.config['EXPIRE_SECONDS'] = server_config.get('expire_seconds', 86400)
 
# 最大上传文件大小（单位：GB → Byte）
max_gb = server_config.get('max_content_length_gb', 1)
app.config['MAX_CONTENT_LENGTH'] = max_gb * 1024 * 1024 * 1024
 
# JSON 输出是否 ASCII
app.config['JSON_AS_ASCII'] = server_config.get('JSON_AS_ASCII', False)
 
# 调试模式
DEBUG_MODE = server_config.get('debuggable', True)
 
# 自动创建上传目录
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# ============================================================
# 2. 日志系统初始化
# ============================================================
LOG_DIR = 'logs'
os.makedirs(LOG_DIR, exist_ok=True)

log_config = config.get('log', DEFAULT_CONFIG['log'])
log_enabled = log_config.get('enabled', True)
log_max_mb = log_config.get('max_file_size_mb', 10)
log_level_str = log_config.get('level', 'INFO').upper()
log_level = LOG_LEVEL_MAP.get(log_level_str, logging.INFO)


def setup_logger():
    """配置日志系统"""
    logger = logging.getLogger('file_server')
    logger.setLevel(log_level)

    # 避免重复添加 handler
    if logger.handlers:
        return logger

    # 日志格式
    formatter = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(module)s.%(funcName)s:%(lineno)d | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # 文件处理器（按大小轮转）
    if log_enabled:
        log_file = os.path.join(LOG_DIR, 'server.log')
        max_bytes = log_max_mb * 1024 * 1024
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=3,
            encoding='utf-8'
        )
        file_handler.setLevel(log_level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    # 控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger


logger = setup_logger()

# 打印当前日志配置
logger.info(f"日志系统初始化完成 | 启用={log_enabled} | 级别={log_level_str} | 最大文件={log_max_mb}MB")

# ============================================================
# 3. 局域网地址路由
# ============================================================
def get_lan_addresses():
    """获取所有非 localhost 的局域网 IP 地址"""
    addresses = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            ip = info[4][0]
            if not ip.startswith('127.') and not ip.startswith('::1') and not ip == '0.0.0.0':
                if ip not in addresses:
                    addresses.append(ip)

        if hasattr(socket, 'AF_INET'):
            import subprocess
            try:
                if os.name == 'nt':  # Windows
                    import locale
                    system_encoding = locale.getpreferredencoding()
                    result = subprocess.run(
                        ['ipconfig'],
                        capture_output=True,
                        text=True,
                        encoding=system_encoding
                    )
                    for line in result.stdout.split('\n'):
                        match = re.search(r'IPv4[^:]*:\s*(\d+\.\d+\.\d+\.\d+)', line)
                        if match:
                            ip = match.group(1)
                            if not ip.startswith('127.'):
                                addresses.append(ip)
                else:  # Linux / macOS
                    result = subprocess.run(['hostname', '-I'], capture_output=True, text=True)
                    if result.returncode == 0:
                        ips = result.stdout.strip().split()
                        for ip in ips:
                            ip = ip.strip()
                            if ip and not ip.startswith('127.'):
                                addresses.append(ip)
            except Exception:
                pass

        addresses = list(dict.fromkeys(addresses))

        if not addresses:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(('10.254.254.254', 1))
                ip = s.getsockname()[0]
                if not ip.startswith('127.'):
                    addresses.append(ip)
            except Exception:
                pass
            finally:
                s.close()
    except Exception as e:
        logger.error(f"获取网络地址失败: {e}")
        return []

    return list(dict.fromkeys(addresses))

@app.route('/api/network-addresses')
def get_network_addresses():
    addresses = get_lan_addresses()
    logger.info(f"获取局域网地址: {addresses}")
    return jsonify({'addresses': addresses})

def get_safe_filename(filename):
    # 保留中文、字母、数字、常见符号，过滤危险路径字符
    filename = re.sub(r'[\\/:*?"<>|\n\r\t]', '', filename)
    # 防止路径穿越攻击，移除上级目录标识
    filename = filename.replace('..', '').lstrip('.')
    # 处理空文件名情况
    if not filename.strip():
        return f"未命名文件_{int(time.time())}"
    return filename


def clean_expired_files():
    """清理过期文件"""
    now = time.time()
    for filename in os.listdir(app.config['UPLOAD_FOLDER']):
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        if os.path.isfile(file_path):
            mtime = os.path.getmtime(file_path)
            if now - mtime > app.config['EXPIRE_SECONDS']:
                os.remove(file_path)
                logger.info(f"清理过期文件: {filename}")


@app.route('/')
def index():
    # 先清理过期文件
    clean_expired_files()
    # 获取已上传的文件列表
    files = []
    now = time.time()
    for filename in os.listdir(app.config['UPLOAD_FOLDER']):
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        if os.path.isfile(file_path):
            size = os.path.getsize(file_path)
            mtime = os.path.getmtime(file_path)
            remain_hour = round((app.config['EXPIRE_SECONDS'] - (now - mtime)) / 3600, 1)
            files.append({
                'name': filename,
                'size': round(size / (1024*1024), 2),
                'remain_hour': remain_hour
            })

    # 获取局域网地址列表
    addresses = get_lan_addresses()

    # 配置信息（用于前端动态显示）
    max_gb = server_config.get('max_content_length_gb', 1)
    expire_hours = app.config['EXPIRE_SECONDS'] // 3600

    logger.debug(f"首页加载，当前文件数: {len(files)}")
    return render_template(
        'index.html',
        files=files,
        addresses=addresses,
        max_gb=max_gb,
        expire_hours=expire_hours,
        port=PORT
    )


@app.route('/upload', methods=['POST'])
def upload_files():
    if 'files' not in request.files:
        logger.warning("上传请求中未包含文件")
        return {'status': 'error', 'msg': '未选择任何文件'}, 400

    files = request.files.getlist('files')
    success_count = 0

    for file in files:
        if file.filename == '':
            continue
        if file:
            filename = get_safe_filename(file.filename)
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            # 重名文件自动加序号
            counter = 1
            while os.path.exists(save_path):
                name, ext = os.path.splitext(filename)
                new_filename = f"{name}_{counter}{ext}"
                save_path = os.path.join(app.config['UPLOAD_FOLDER'], new_filename)
                counter += 1
            file.save(save_path)
            success_count += 1
            logger.info(f"文件上传成功: {os.path.basename(save_path)}")

    logger.info(f"批量上传完成: 成功{success_count}个文件")
    return {
        'status': 'success',
        'msg': f'成功上传{success_count}个文件'
    }, 200


@app.route('/download/<filename>')
def download_file(filename):
    # 下载前检查文件是否过期
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], get_safe_filename(filename))
    if not os.path.exists(file_path):
        logger.warning(f"下载失败，文件不存在: {filename}")
        return "文件不存在或已过期", 404
    if time.time() - os.path.getmtime(file_path) > app.config['EXPIRE_SECONDS']:
        os.remove(file_path)
        logger.info(f"下载时发现文件已过期并删除: {filename}")
        return "文件已过期", 410
    logger.info(f"文件下载: {filename}")
    return send_from_directory(
        app.config['UPLOAD_FOLDER'],
        get_safe_filename(filename),
        as_attachment=True
    )


@app.route('/delete/<filename>', methods=['POST'])
def delete_file(filename):
    safe_name = get_safe_filename(filename)
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], safe_name)
    if os.path.exists(file_path) and os.path.isfile(file_path):
        os.remove(file_path)
        logger.info(f"文件删除成功: {safe_name}")
        return jsonify({'status': 'success', 'msg': '删除成功'})
    logger.warning(f"删除失败，文件不存在: {safe_name}")
    return jsonify({'status': 'error', 'msg': '文件不存在'}), 404


@app.route('/batch-delete', methods=['POST'])
def batch_delete():
    filenames = request.json.get('filenames', [])
    if not filenames:
        return jsonify({'status': 'error', 'msg': '未选择任何文件'}), 400

    delete_count = 0
    for filename in filenames:
        safe_name = get_safe_filename(filename)
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], safe_name)
        if os.path.exists(file_path) and os.path.isfile(file_path):
            os.remove(file_path)
            delete_count += 1
            logger.info(f"批量删除文件: {safe_name}")

    logger.info(f"批量删除完成: 共删除{delete_count}个文件")
    return jsonify({
        'status': 'success',
        'msg': f'成功删除{delete_count}个文件'
    })


@app.route('/batch-download', methods=['POST'])
def batch_download():
    # 获取选中的文件名列表
    filenames = request.json.get('filenames', [])
    if not filenames:
        return jsonify({'status': 'error', 'msg': '未选择任何文件'}), 400

    # 内存中创建压缩包
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as zip_file:
        for filename in filenames:
            safe_name = get_safe_filename(filename)
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], safe_name)
            if os.path.exists(file_path) and os.path.isfile(file_path):
                # 修复ZIP包中文文件名乱码：设置UTF-8编码标识
                zinfo = zipfile.ZipInfo(safe_name)
                zinfo.flag_bits |= 0x800  # 标识文件名使用UTF-8编码
                zinfo.date_time = time.localtime(os.path.getmtime(file_path))[:6]
                zinfo.create_system = 3  # 标识为Unix系统，提升兼容性
                with open(file_path, 'rb') as f:
                    zip_file.writestr(zinfo, f.read())

    zip_buffer.seek(0)
    # 修复响应头中文文件名编码错误：对文件名做URL编码
    zip_filename = quote(f"批量下载_{int(time.time())}.zip")
    # 生成响应头，兼容所有浏览器的中文文件名识别
    response = Response(
        zip_buffer,
        mimetype='application/zip',
        headers={
            'Content-Disposition': f"attachment; filename*=UTF-8''{zip_filename}; filename={zip_filename}",
            'Access-Control-Expose-Headers': 'Content-Disposition'
        }
    )
    logger.info(f"批量下载: {len(filenames)}个文件打包为ZIP")
    return response


@app.route('/api/files')
def get_file_list():
    """返回JSON格式的文件列表，用于前端局部刷新"""
    clean_expired_files()
    files = []
    now = time.time()
    for filename in os.listdir(app.config['UPLOAD_FOLDER']):
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        if os.path.isfile(file_path):
            size = os.path.getsize(file_path)
            mtime = os.path.getmtime(file_path)
            remain_hour = round((app.config['EXPIRE_SECONDS'] - (now - mtime)) / 3600, 1)
            files.append({
                'name': filename,
                'size': round(size / (1024*1024), 2),
                'remain_hour': remain_hour
            })
    return jsonify(files)

@app.route('/api/test')
def api_test():
    """用于前端验证是否为当前工具的页面"""
    return jsonify({'status': 'ok', 'app': 'lan-file-share'})

# ============================================================
# API: 一键打开本地上传文件夹
# ============================================================
@app.route('/api/open-upload-folder', methods=['GET'])
def open_upload_folder():
    """
    一键打开服务器本地的上传文件夹（在服务器端弹出文件管理器）
    适用于管理员在服务器上调试或快速查看上传文件。
    """
    try:
        upload_path = os.path.abspath(app.config['UPLOAD_FOLDER'])
 
        if not os.path.exists(upload_path):
            return {
                "code": 404,
                "message": f"上传文件夹不存在: {upload_path}",
                "data": None
            }, 404
 
        system = platform.system()
 
        if system == 'Windows':
            # Windows: 用 explorer 打开
            subprocess.Popen(['explorer', upload_path], shell=True)
        elif system == 'Darwin':
            # macOS: 用 open 打开
            subprocess.Popen(['open', upload_path])
        elif system == 'Linux':
            # Linux: 尝试多种文件管理器
            file_managers = [
                'xdg-open',      # 通用默认打开方式
                'nautilus',      # GNOME
                'dolphin',       # KDE
                'nemo',          # Cinnamon
                'pcmanfm',       # LXDE / LXQt
                'thunar',        # XFCE
            ]
            opened = False
            for fm in file_managers:
                try:
                    subprocess.Popen([fm, upload_path])
                    opened = True
                    break
                except FileNotFoundError:
                    continue
            if not opened:
                return {
                    "code": 500,
                    "message": "无法在Linux上找到可用的文件管理器",
                    "data": None
                }, 500
        else:
            return {
                "code": 400,
                "message": f"不支持的操作系统: {system}",
                "data": None
            }, 400
 
        return {
            "code": 200,
            "message": f"已打开上传文件夹: {upload_path}",
            "data": {
                "path": upload_path,
                "system": system
            }
        }, 200
 
    except Exception as e:
        return {
            "code": 500,
            "message": f"打开文件夹失败: {str(e)}",
            "data": None
        }, 500

if __name__ == '__main__':
    # 打印可访问的局域网地址
    logger.info("=" * 60)
    logger.info("文件共享服务器启动中...")
    logger.info(f"本地访问: http://127.0.0.1:{PORT}")
    logger.info(f"局域网地址: http://<your-lan-ip>:{PORT}")
    logger.info("可通过 GET /api/network-addresses 获取所有可用局域网地址")
    logger.info("=" * 60)
    # 绑定0.0.0.0允许局域网内其他设备访问
    app.run(host='0.0.0.0', port=PORT, debug=DEBUG_MODE)