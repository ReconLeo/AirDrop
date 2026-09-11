# -*- coding: utf-8 -*-
"""
AirDrop 局域网文件共享插件（airdrop）
=====================================
从独立 Flask 应用 AirDrop（simple-airdrop.py）迁移的局域网文件共享能力：

- 文件传输：上传（多文件/重名加序号/大小限制）/ 下载 / 删除 / 批量删除 / 批量下载 zip
- 文件列表：JSON 接口（前端局部刷新）+ 过期文件清理
- 网络信息：局域网 IP 列表（页面二维码 / 访问地址）
- 系统辅助：服务端打开上传文件夹（仅管理员）
- 安全防护：get_safe_filename 路径穿越防护、zip 中文名 UTF-8 兼容、下载头中文名编码

配置（plugins/configs/airdrop.json）：
- upload_folder  : 上传目录绝对路径（默认指向 AirDrop 原 uploads，零迁移风险）
- expire_seconds : 文件有效期（秒），过期自动清理（定时任务 30min + 请求时兜底）
- max_gb         : 单文件大小上限（GB）；v4.2.2 起映射为插件级 max_upload_size（MB），
                    upload 路由声明 route 级 max_upload 突破全局默认，保存前由框架统一预检（流 seek/tell 不落盘）
- auth_required  : 双模式鉴权开关（false=免登录即开即用；true=需部署框架 auth 插件按权限矩阵执行）

鉴权（可配置双模式，与框架"鉴权可选"机制对齐）：
- 路由按真实权限矩阵标记（读 public / 上传 user / 删除·打开文件夹 admin）
- 框架原生：auth 插件未安装时全部放行 → 默认免登录
- 安装 auth 插件后按权限矩阵自动鉴权（登录态 + CSRF + 角色）

依赖：无（标准库 + Flask）
"""
import os
import re
import time
import zipfile
import logging
import socket
import subprocess
import platform
from io import BytesIO
from typing import List, Dict
from urllib.parse import quote

from flask import jsonify, request, Response, send_from_directory

import global_var
from plugins.base_plugin import BasePlugin, permission as permission_required


class AirDropPlugin(BasePlugin):
    name = "airdrop"
    title = "AirDrop 局域网文件共享"
    author = "AirDrop"
    version = "1.0.0"
    category = "文件工具"
    description = ("局域网文件共享：上传/下载/删除/批量操作/过期清理/局域网地址，"
                   "可配置双模式鉴权。数据目录经配置指向原 uploads。")
    permission = "user"

    # ------------------------------------------------------------------
    # 定时任务：过期文件清理（每 30 分钟；另有 /files 请求时兜底清理）
    # ------------------------------------------------------------------
    @property
    def scheduled_tasks(self) -> List[Dict]:
        return [{
            "func": self.clean_expired_files,
            "trigger": "interval",
            "minutes": 30,
        }]

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------
    # 所有声明路由权限的方法（用于免登录模式下动态降级为 public）
    _ROUTE_METHODS = (
        'get_network_addresses', 'get_file_list', 'upload_files', 'download_file',
        'delete_file', 'batch_delete', 'batch_download', 'api_test', 'open_upload_folder',
    )
    # 跨实例缓存装饰器声明的原始权限（reload 切换鉴权模式时可恢复，防止被 public 永久污染）
    _ORIG_PERM = {}

    def __init__(self):
        super().__init__()
        self.upload_folder = self.config.get("upload_folder", "uploads")
        self.expire_seconds = int(self.config.get("expire_seconds", 86400))
        self.max_gb = float(self.config.get("max_gb", 1))
        self.auth_required = bool(self.config.get("auth_required", False))
        self.upload_folder_abs = os.path.abspath(self.upload_folder)
        os.makedirs(self.upload_folder, exist_ok=True)
        # 双模式鉴权落地：auth_required=false 时所有路由降级 public（框架完全不拦截，
        # 即使已装 auth 插件也免登录）；true 时保持装饰器声明的权限矩阵（读 public/写 user/admin）。
        self._apply_auth_mode()
        # 页面级公开标记：与免登录模式联动，供框架 interceptor 的 /plugin/ 守卫豁免（public_page=True）
        self.public_page = not self.auth_required
        self.logger.info(
            f"AirDrop 初始化: upload_folder={self.upload_folder_abs} | "
            f"expire={self.expire_seconds}s | max={self.max_gb}GB | auth_required={self.auth_required}")

    def _apply_auth_mode(self):
        """双模式鉴权落地：
        - auth_required=false：路由方法 _permission 降级为 public（框架完全不拦截，免登录）
        - auth_required=true ：恢复装饰器声明的原始权限矩阵
        wrap_view_func 在加载时读取 __func__._permission，故在此处（实例化时）改写生效。"""
        for name in self._ROUTE_METHODS:
            func = getattr(self, name)
            if not hasattr(func, '__func__'):
                continue
            f = func.__func__
            if name not in AirDropPlugin._ORIG_PERM:
                AirDropPlugin._ORIG_PERM[name] = getattr(f, '_permission', None)
            if self.auth_required:
                f._permission = AirDropPlugin._ORIG_PERM[name]
            else:
                f._permission = 'public'
        mode = '鉴权模式（按权限矩阵）' if self.auth_required else '免登录模式（全部 public）'
        self.logger.info(f"AirDrop 当前鉴权模式: {mode}")

    def on_load(self):
        """启动一致性校验：auth_required=true 但框架未装 auth 插件时告警（此时无法按矩阵鉴权）。"""
        if self.auth_required and "auth" not in global_var.plugins:
            self.logger.warning(
                f"AirDrop 配置 auth_required=true 但框架未安装 auth 插件，权限矩阵不会生效"
                f"（当前所有接口实际免登录）！请部署 auth 插件。")
        elif self.auth_required and "auth" in global_var.plugins:
            self.logger.info(
                f"AirDrop 鉴权模式已生效：按权限矩阵执行（读 public / 上传 user / 删除·打开文件夹 admin）")
        else:
            self.logger.info("AirDrop 免登录模式已生效（auth_required=false，全部接口 public）")

    def on_ready(self):
        """就绪回调（v4.2.2）：所有插件加载完成后确认依赖，避免拓扑序误报。
        on_load 阶段 auth 可能尚未加载（warning 语义）；此处 auth 状态已确定。
        严格模式（PLUGIN_STRICT_MODE）下此处做最终确认，缺失则升级为 error 记录。"""
        if not self.auth_required:
            return
        import global_var as _gv
        if "auth" not in _gv.plugins:
            msg = ("AirDrop 配置 auth_required=true 但框架未安装 auth 插件，"
                   "权限矩阵不会生效（当前所有接口实际免登录）！请部署 auth 插件。")
            if getattr(_gv, 'PLUGIN_STRICT_MODE', False):
                self.logger.error(msg)  # 严格模式：最终确认缺失 → error 记录
            else:
                self.logger.warning(msg)
        else:
            self.logger.info("AirDrop 鉴权模式已确认生效（auth 已加载，按权限矩阵执行）")

    # ------------------------------------------------------------------
    # 路由声明
    # ------------------------------------------------------------------
    @property
    def max_upload_size(self):
        """插件级上传上限（MB，v4.2.2 统一机制）：映射自 config max_gb（GB），向后兼容。"""
        return int(self.max_gb * 1024)  # GB → MB

    @property
    def routes(self) -> List[Dict]:
        return [
            {"path": "/network-addresses", "methods": ["GET"], "view_func": self.get_network_addresses},
            {"path": "/files", "methods": ["GET"], "view_func": self.get_file_list},
            # route 级 max_upload（MB）：提升本请求 MAX_CONTENT_LENGTH 上限，突破全局默认（100MB）
            {"path": "/upload", "methods": ["POST"], "view_func": self.upload_files,
             "max_upload": int(self.max_gb * 1024)},
            {"path": "/download/<filename>", "methods": ["GET"], "view_func": self.download_file},
            {"path": "/delete/<filename>", "methods": ["POST"], "view_func": self.delete_file},
            {"path": "/batch-delete", "methods": ["POST"], "view_func": self.batch_delete},
            {"path": "/batch-download", "methods": ["POST"], "view_func": self.batch_download},
            {"path": "/test", "methods": ["GET"], "view_func": self.api_test},
            {"path": "/open-upload-folder", "methods": ["GET"], "view_func": self.open_upload_folder},
        ]

    # ------------------------------------------------------------------
    # 安全文件名（路径穿越防护，原样保留）
    # ------------------------------------------------------------------
    @staticmethod
    def get_safe_filename(filename):
        # 保留中文、字母、数字、常见符号，过滤危险路径字符
        filename = re.sub(r'[\\/:*?"<>|\n\r\t]', '', filename)
        # 防止路径穿越攻击，移除上级目录标识
        filename = filename.replace('..', '').lstrip('.')
        # 处理空文件名情况
        if not filename.strip():
            return f"未命名文件_{int(time.time())}"
        return filename

    # ------------------------------------------------------------------
    # 局域网地址
    # ------------------------------------------------------------------
    def get_lan_addresses(self):
        """获取所有非 localhost 的局域网 IP 地址"""
        addresses = []
        try:
            hostname = socket.gethostname()
            for info in socket.getaddrinfo(hostname, None):
                ip = info[4][0]
                if not ip.startswith('127.') and not ip.startswith('::1') and not ip == '0.0.0.0':
                    if ip not in addresses:
                        addresses.append(ip)

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
                        for ip in result.stdout.strip().split():
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
            self.logger.error(f"获取网络地址失败: {e}")
            return []

        return list(dict.fromkeys(addresses))

    @permission_required("public")
    def get_network_addresses(self):
        addresses = self.get_lan_addresses()
        self.logger.info(f"获取局域网地址: {addresses}")
        return jsonify({'addresses': addresses})

    # ------------------------------------------------------------------
    # 过期文件清理（定时任务 + 请求时兜底）
    # ------------------------------------------------------------------
    def clean_expired_files(self):
        """清理过期文件"""
        now = time.time()
        cleaned = 0
        for filename in os.listdir(self.upload_folder):
            file_path = os.path.join(self.upload_folder, filename)
            if os.path.isfile(file_path):
                mtime = os.path.getmtime(file_path)
                if now - mtime > self.expire_seconds:
                    try:
                        os.remove(file_path)
                        cleaned += 1
                        self.logger.info(f"清理过期文件: {filename}")
                    except OSError as e:
                        self.logger.warning(f"清理过期文件失败 {filename}: {e}")
        if cleaned:
            self.logger.info(f"过期清理完成，共删除 {cleaned} 个文件")
        return cleaned

    # ------------------------------------------------------------------
    # 主页面 index.html 数据注入（/plugin/airdrop）
    # ------------------------------------------------------------------
    def render_index(self):
        """主页面数据注入钩子：文件列表 + 局域网地址 + 配置信息。
        端口不注入——前端已改为 JS 动态取 location.port，适配框架动态端口。"""
        self.clean_expired_files()
        return {
            'files': self._list_files(),
            'addresses': self.get_lan_addresses(),
            'max_gb': self.max_gb,
            'expire_hours': self.expire_seconds // 3600,
        }

    # ------------------------------------------------------------------
    # 文件列表（JSON，前端局部刷新）
    # ------------------------------------------------------------------
    def _list_files(self):
        """返回文件列表（name/size MB/remain_hour）"""
        files = []
        now = time.time()
        for filename in os.listdir(self.upload_folder):
            file_path = os.path.join(self.upload_folder, filename)
            if os.path.isfile(file_path):
                size = os.path.getsize(file_path)
                mtime = os.path.getmtime(file_path)
                remain_hour = round((self.expire_seconds - (now - mtime)) / 3600, 1)
                files.append({
                    'name': filename,
                    'size': round(size / (1024 * 1024), 2),
                    'remain_hour': remain_hour
                })
        return files

    @permission_required("public")
    def get_file_list(self):
        self.clean_expired_files()
        return jsonify(self._list_files())

    # ------------------------------------------------------------------
    # 上传
    # ------------------------------------------------------------------
    @permission_required("user")
    def upload_files(self):
        if 'files' not in request.files:
            self.logger.warning("上传请求中未包含文件")
            return {'status': 'error', 'msg': '未选择任何文件'}, 400

        files = request.files.getlist('files')
        success_count = 0
        # 统一预检：route 级 max_upload 已提升本请求 MAX_CONTENT_LENGTH；
        # 这里对每个文件用流 seek/tell 预检（不落盘），超限即拒
        for file in files:
            if not file or file.filename == '':
                continue
            oversize = self.check_upload_limit(file)
            if oversize:
                self.logger.warning(f"上传被拒（统一预检超限）: {file.filename}")
                return {'status': 'error', 'msg': f'文件大小超过限制（{self.max_gb}GB）'}, 413
            filename = self.get_safe_filename(file.filename)
            save_path = os.path.join(self.upload_folder, filename)
            # 重名文件自动加序号
            counter = 1
            while os.path.exists(save_path):
                name, ext = os.path.splitext(filename)
                new_filename = f"{name}_{counter}{ext}"
                save_path = os.path.join(self.upload_folder, new_filename)
                counter += 1
            file.save(save_path)
            success_count += 1
            self.logger.info(f"文件上传成功: {os.path.basename(save_path)}")

        self.logger.info(f"批量上传完成: 成功{success_count}个文件")
        return {'status': 'success', 'msg': f'成功上传{success_count}个文件'}, 200

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------
    @permission_required("public")
    def download_file(self, filename):
        # 下载前检查文件是否过期
        safe_name = self.get_safe_filename(filename)
        file_path = os.path.join(self.upload_folder, safe_name)
        if not os.path.exists(file_path):
            self.logger.warning(f"下载失败，文件不存在: {filename}")
            return "文件不存在或已过期", 404
        if time.time() - os.path.getmtime(file_path) > self.expire_seconds:
            try:
                os.remove(file_path)
            except OSError:
                pass
            self.logger.info(f"下载时发现文件已过期并删除: {filename}")
            return "文件已过期", 410
        self.logger.info(f"文件下载: {filename}")
        # v4.2.2：统一 send_file_response（中文文件名 RFC 5987 编码 + 下载统计计入插件热度）
        return self.send_file_response(
            os.path.join(self.upload_folder, safe_name),
            download_name=safe_name,
            stats_endpoint=f"/download/{safe_name}",
        )

    # ------------------------------------------------------------------
    # 删除 / 批量删除
    # ------------------------------------------------------------------
    @permission_required("admin")
    def delete_file(self, filename):
        safe_name = self.get_safe_filename(filename)
        file_path = os.path.join(self.upload_folder, safe_name)
        if os.path.exists(file_path) and os.path.isfile(file_path):
            try:
                os.remove(file_path)
            except OSError as e:
                self.logger.error(f"删除失败: {safe_name}: {e}")
                return jsonify({'status': 'error', 'msg': f'删除失败: {e}'}), 500
            self.logger.info(f"文件删除成功: {safe_name}")
            return jsonify({'status': 'success', 'msg': '删除成功'})
        self.logger.warning(f"删除失败，文件不存在: {safe_name}")
        return jsonify({'status': 'error', 'msg': '文件不存在'}), 404

    @permission_required("admin")
    def batch_delete(self):
        filenames = request.json.get('filenames', []) if request.is_json else []
        if not filenames:
            return jsonify({'status': 'error', 'msg': '未选择任何文件'}), 400

        delete_count = 0
        for filename in filenames:
            safe_name = self.get_safe_filename(filename)
            file_path = os.path.join(self.upload_folder, safe_name)
            if os.path.exists(file_path) and os.path.isfile(file_path):
                try:
                    os.remove(file_path)
                    delete_count += 1
                    self.logger.info(f"批量删除文件: {safe_name}")
                except OSError as e:
                    self.logger.error(f"批量删除失败: {safe_name}: {e}")

        self.logger.info(f"批量删除完成: 共删除{delete_count}个文件")
        return jsonify({
            'status': 'success',
            'msg': f'成功删除{delete_count}个文件'
        })

    # ------------------------------------------------------------------
    # 批量下载（zip，中文名 UTF-8 兼容）
    # ------------------------------------------------------------------
    @permission_required("public")
    def batch_download(self):
        filenames = request.json.get('filenames', []) if request.is_json else []
        if not filenames:
            return jsonify({'status': 'error', 'msg': '未选择任何文件'}), 400

        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as zip_file:
            for filename in filenames:
                safe_name = self.get_safe_filename(filename)
                file_path = os.path.join(self.upload_folder, safe_name)
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
        response = Response(
            zip_buffer,
            mimetype='application/zip',
            headers={
                'Content-Disposition': f"attachment; filename*=UTF-8''{zip_filename}; filename={zip_filename}",
                'Access-Control-Expose-Headers': 'Content-Disposition'
            }
        )
        self.logger.info(f"批量下载: {len(filenames)}个文件打包为ZIP")
        return response

    # ------------------------------------------------------------------
    # 存活自检
    # ------------------------------------------------------------------
    @permission_required("public")
    def api_test(self):
        """用于前端验证是否为当前工具的页面"""
        return jsonify({'status': 'ok', 'app': 'lan-file-share'})

    # ------------------------------------------------------------------
    # 一键打开本地上传文件夹（服务端，仅管理员）
    # ------------------------------------------------------------------
    @permission_required("admin")
    def open_upload_folder(self):
        try:
            upload_path = os.path.abspath(self.upload_folder)

            if not os.path.exists(upload_path):
                return {
                    "code": 404,
                    "message": f"上传文件夹不存在: {upload_path}",
                    "data": None
                }, 404

            system = platform.system()

            if system == 'Windows':
                subprocess.Popen(['explorer', upload_path], shell=True)
            elif system == 'Darwin':
                subprocess.Popen(['open', upload_path])
            elif system == 'Linux':
                file_managers = ['xdg-open', 'nautilus', 'dolphin', 'nemo', 'pcmanfm', 'thunar']
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
                "data": {"path": upload_path, "system": system}
            }, 200

        except Exception as e:
            return {
                "code": 500,
                "message": f"打开文件夹失败: {str(e)}",
                "data": None
            }, 500
