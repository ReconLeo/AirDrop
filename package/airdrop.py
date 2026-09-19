# -*- coding: utf-8 -*-
"""
AirDrop 局域网文件共享插件（airdrop）
=====================================
从独立 Flask 应用 AirDrop（simple-airdrop.py）迁移的局域网文件共享能力：

- 文件传输：上传（多文件/重名加序号/大小限制）/ 下载 / 删除 / 批量删除 / 批量下载 zip
- 文件列表：JSON 接口（前端局部刷新）+ 过期文件清理
- 网络信息：局域网 IP 列表（页面二维码 / 访问地址）
- 系统辅助：服务端打开上传文件夹（仅管理员）
- 安全防护：sanitize_filename 路径穿越防护、zip 中文名 UTF-8 兼容、下载头中文名编码

配置（plugins/configs/airdrop.json）：
- upload_folder  : 上传目录绝对路径（默认指向 AirDrop 原 uploads，零迁移风险）
- expire_seconds : 文件有效期（秒），过期自动清理（定时任务 30min + 请求时兜底）
- max_gb         : 单文件大小上限（GB）；v4.2.2 起映射为插件级 max_upload_size（MB），
                    upload 路由声明 route 级 max_upload 突破全局默认，保存前由框架统一预检（流 seek/tell 不落盘）
- auth_required  : 双模式鉴权开关（false=免登录即开即用；true=需部署框架 auth 插件按权限矩阵执行）

断点续传（v1.4.0，大文件分片上传 + 合并；下载续传由 Flask send_file 的 HTTP Range 天然支持）：
- chunk_size_mb        : 单分片大小上限（MB，默认 8）；大文件（>该值）走分片上传，小文件走原整文件上传
- chunk_expire_minutes : 临时分片保留时长（分钟，默认 30），超时未合并的分片由定时任务清理
分片临时目录：<upload_folder>/.chunks/<file_id>/（file_id 由前端生成，客户端本地记录以便断点续传）

鉴权（可配置双模式，与框架"鉴权可选"机制对齐）：
- 路由按真实权限矩阵标记（读 public / 上传 user / 删除·打开文件夹 admin）
- 框架原生：auth 插件未安装时全部放行 → 默认免登录
- 安装 auth 插件后按权限矩阵自动鉴权（登录态 + CSRF + 角色）

依赖：无（标准库 + Flask）
"""
import os
import re
import json
import time
import shutil
import uuid
import zipfile
import logging
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
    version = "1.5.0"
    category = "文件工具"
    description = ("局域网文件共享：上传/下载/删除/批量操作/过期清理/局域网地址，"
                   "可配置双模式鉴权。大文件支持分片上传与断点续传（下载续传由 HTTP Range 天然支持）。"
                   "数据目录经配置指向原 uploads。")
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
        }, {
            "func": self.clean_expired_chunks,
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
        'upload_chunk', 'chunk_status', 'complete_upload', 'abort_upload',
    )
    # 跨实例缓存装饰器声明的原始权限（reload 切换鉴权模式时可恢复，防止被 public 永久污染）
    _ORIG_PERM = {}

    def __init__(self):
        super().__init__()
        self.upload_folder = self.config.get("upload_folder", "uploads")
        self.expire_seconds = int(self.config.get("expire_seconds", 86400))
        self.max_gb = float(self.config.get("max_gb", 1))
        self.auth_required = bool(self.config.get("auth_required", False))
        # 分片上传（断点续传）：单分片大小上限 + 临时分片保留时长
        self.chunk_size_mb = float(self.config.get("chunk_size_mb", 8))
        self.chunk_expire_minutes = int(self.config.get("chunk_expire_minutes", 30))
        self.upload_folder_abs = os.path.abspath(self.upload_folder)
        os.makedirs(self.upload_folder, exist_ok=True)
        # 分片临时目录（子目录 .chunks 不会作为普通文件出现在文件列表）
        self.chunk_folder = os.path.join(self.upload_folder, ".chunks")
        os.makedirs(self.chunk_folder, exist_ok=True)
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
            # 分片上传（断点续传）：单分片请求上限 = chunk_size_mb + 1MB 余量
            {"path": "/upload/chunk", "methods": ["POST"], "view_func": self.upload_chunk,
             "max_upload": int(self.chunk_size_mb) + 1},
            {"path": "/upload/status", "methods": ["GET"], "view_func": self.chunk_status},
            {"path": "/upload/complete", "methods": ["POST"], "view_func": self.complete_upload},
            {"path": "/upload/abort", "methods": ["POST"], "view_func": self.abort_upload},
            {"path": "/download/<filename>", "methods": ["GET"], "view_func": self.download_file},
            {"path": "/delete/<filename>", "methods": ["POST"], "view_func": self.delete_file},
            {"path": "/batch-delete", "methods": ["POST"], "view_func": self.batch_delete},
            {"path": "/batch-download", "methods": ["POST"], "view_func": self.batch_download},
            {"path": "/test", "methods": ["GET"], "view_func": self.api_test},
            {"path": "/open-upload-folder", "methods": ["GET"], "view_func": self.open_upload_folder},
        ]

    # ------------------------------------------------------------------
    # 局域网地址
    # ------------------------------------------------------------------
    def get_lan_addresses(self):
        """获取所有非 localhost 的局域网 IP（复用框架 core.network，消除 subprocess/socket 高风险）"""
        try:
            from core.network import get_lan_addresses as _get_lan_addresses
            return _get_lan_addresses()
        except Exception as e:
            self.logger.error(f"获取网络地址失败: {e}")
            return []

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
            'chunk_size_mb': self.chunk_size_mb,
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
        # 同步持久化上传助手（v4.18 save_uploads）：净化 + 重名去重 + 大小/配额双预检 + 落盘一次完成
        try:
            results = self.save_uploads('files', dest_dir=self.upload_folder)
        except ValueError as e:
            self.logger.warning(f"上传请求无效: {e}")
            return {'status': 'error', 'msg': str(e)}, 400
        if not results:
            return {'status': 'error', 'msg': '未选择任何文件'}, 400

        rejected = [r for r in results if r['status'] == 'rejected']
        if rejected:
            r0 = rejected[0]
            reason = r0.get('reason')
            if reason == 'quota_exceeded':
                remaining = r0.get('remaining_mb')
                msg = '存储空间不足' + (f'（剩余 {remaining:.1f}MB）' if remaining is not None else '')
                self.logger.warning(f"上传被拒（存储配额）: {r0['original_name']}")
                return {'status': 'error', 'msg': msg}, 413
            if reason == 'size_exceeded':
                self.logger.warning(f"上传被拒（大小超限）: {r0['original_name']}")
                return {'status': 'error', 'msg': f'文件大小超过限制（{self.max_gb}GB）'}, 413
            if reason == 'invalid_type':
                return {'status': 'error', 'msg': '不支持的文件类型'}, 400
            return {'status': 'error', 'msg': '文件上传失败'}, 400

        success = len([r for r in results if r['status'] == 'saved'])
        self.logger.info(f"批量上传完成: 成功{success}个文件")
        return {'status': 'success', 'msg': f'成功上传{success}个文件'}, 200

    # ------------------------------------------------------------------
    # 分片上传（断点续传）
    # 协议：前端把大文件切成若干 chunk 逐个 POST /upload/chunk，
    #       客户端本地记录 file_id，续传前先 GET /upload/status 查已接收分片、只补缺失；
    #       全部到位后 POST /upload/complete 由服务端按 chunk_index 排序合并落盘。
    # 临时目录：<upload_folder>/.chunks/<file_id>/，含分片 .part 与元数据 meta.json
    # ------------------------------------------------------------------
    _CHUNK_ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')

    def _safe_chunk_id(self, file_id: str) -> str:
        """净化 file_id：仅允许字母/数字/下划线/连字符，限长 64，防路径穿越。"""
        fid = (file_id or '').strip()
        if not self._CHUNK_ID_RE.match(fid):
            raise ValueError('file_id 无效')
        return fid

    def _chunk_dir(self, file_id: str) -> str:
        """分片临时目录（file_id 已净化，安全拼路径）。"""
        return os.path.join(self.chunk_folder, self._safe_chunk_id(file_id))

    def _chunk_meta_path(self, cdir: str) -> str:
        return os.path.join(cdir, 'meta.json')

    def _read_chunk_meta(self, cdir: str) -> dict:
        """读取分片元数据（total_chunks / last_active），文件缺失时返回空 dict。"""
        try:
            with open(self._chunk_meta_path(cdir), 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}

    def _write_chunk_meta(self, cdir: str, total_chunks: int):
        """写入/刷新分片元数据（total_chunks + 最近活动时间，供超时清理）。"""
        os.makedirs(cdir, exist_ok=True)
        meta = self._read_chunk_meta(cdir)
        meta['total_chunks'] = int(total_chunks)
        meta['last_active'] = time.time()
        tmp = self._chunk_meta_path(cdir) + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False)
        os.replace(tmp, self._chunk_meta_path(cdir))

    @permission_required("user")
    def upload_chunk(self):
        """接收单个分片：multipart 字段 file_id / chunk_index / total_chunks / chunk(文件)。"""
        file_id = request.form.get('file_id', '')
        try:
            chunk_index = int(request.form.get('chunk_index', -1))
            total_chunks = int(request.form.get('total_chunks', 0))
        except ValueError:
            return jsonify({'status': 'error', 'msg': '分片参数无效'}), 400
        if not file_id or chunk_index < 0 or total_chunks <= 0 or chunk_index >= total_chunks:
            return jsonify({'status': 'error', 'msg': '分片参数无效'}), 400
        if 'chunk' not in request.files:
            return jsonify({'status': 'error', 'msg': '缺少分片数据'}), 400
        try:
            cdir = self._chunk_dir(file_id)
        except ValueError:
            return jsonify({'status': 'error', 'msg': 'file_id 无效'}), 400
        os.makedirs(cdir, exist_ok=True)
        chunk = request.files['chunk']
        part = os.path.join(cdir, f'{chunk_index}.part')
        chunk.save(part)
        # 分片大小预检（落盘后按实际字节校验，规避流 seek/tell 与保存的冲突）
        if os.path.getsize(part) > self.chunk_size_mb * 1024 * 1024:
            try:
                os.remove(part)
            except OSError:
                pass
            self.logger.warning(f"分片超限被拒: {file_id}#{chunk_index}")
            return jsonify({'status': 'error', 'msg': f'分片超过大小限制（{self.chunk_size_mb}MB）'}), 413
        self._write_chunk_meta(cdir, total_chunks)
        self.logger.info(f"分片已接收: {file_id}#{chunk_index}/{total_chunks}")
        return jsonify({'status': 'ok', 'msg': '分片已接收'}), 200

    @permission_required("public")
    def chunk_status(self):
        """断点续传核心：返回该 file_id 已接收的分片索引与总分片数，前端只补缺失分片。"""
        file_id = request.args.get('file_id', '')
        if not file_id:
            return jsonify({'status': 'error', 'msg': '缺少 file_id'}), 400
        try:
            cdir = self._chunk_dir(file_id)
        except ValueError:
            return jsonify({'status': 'error', 'msg': 'file_id 无效'}), 400
        received, total_chunks = [], 0
        meta = self._read_chunk_meta(cdir)
        total_chunks = meta.get('total_chunks', 0)
        if os.path.isdir(cdir):
            for fn in os.listdir(cdir):
                if fn.endswith('.part'):
                    try:
                        received.append(int(fn[:-5]))
                    except ValueError:
                        pass
        received.sort()
        return jsonify({'status': 'ok', 'file_id': file_id,
                        'received': received, 'total_chunks': total_chunks})

    @permission_required("user")
    def complete_upload(self):
        """合并分片：校验完整性 → 净化/去重命名 → 配额预检 → 按索引排序合并 → 清理临时目录。"""
        file_id = request.form.get('file_id', '')
        filename = request.form.get('filename', '')
        try:
            total_chunks = int(request.form.get('total_chunks', 0))
        except ValueError:
            total_chunks = 0
        if not file_id or not filename or total_chunks <= 0:
            return jsonify({'status': 'error', 'msg': '参数无效'}), 400
        try:
            cdir = self._chunk_dir(file_id)
        except ValueError:
            return jsonify({'status': 'error', 'msg': 'file_id 无效'}), 400
        if not os.path.isdir(cdir):
            return jsonify({'status': 'error', 'msg': '分片不存在，无法合并'}), 404
        # 1) 完整性校验：收集 0..total_chunks-1 全部分片
        parts = {}
        if os.path.isdir(cdir):
            for fn in os.listdir(cdir):
                if fn.endswith('.part'):
                    try:
                        parts[int(fn[:-5])] = os.path.join(cdir, fn)
                    except ValueError:
                        pass
        missing = [i for i in range(total_chunks) if i not in parts]
        if missing:
            self.logger.warning(f"分片不完整，拒绝合并: {file_id} 缺 {len(missing)} 块")
            return jsonify({'status': 'error', 'msg': f'分片不完整，缺失 {len(missing)} 块'}), 400
        # 2) 净化文件名 + 重名去重（与 save_uploads 的 name_1.ext 策略一致）
        saved_name = self.sanitize_filename(filename)
        if not saved_name or saved_name in ('.', '..'):
            return jsonify({'status': 'error', 'msg': '文件名无效'}), 400
        base_name, ext = os.path.splitext(saved_name)
        save_path = os.path.join(self.upload_folder, saved_name)
        n = 1
        while os.path.exists(save_path):
            saved_name = f'{base_name}_{n}{ext}'
            save_path = os.path.join(self.upload_folder, saved_name)
            n += 1
        # 3) 配额预检（合并后总大小，复用框架 check_upload）
        total_size = sum(os.path.getsize(parts[i]) for i in range(total_chunks))
        quota = self.check_upload(total_size)
        if not quota.get('ok'):
            remaining = quota.get('remaining_mb')
            msg = '存储空间不足' + (f'（剩余 {remaining:.1f}MB）' if remaining is not None else '')
            self.logger.warning(f"合并被拒（存储配额）: {saved_name}")
            return jsonify({'status': 'error', 'msg': msg}), 413
        # 4) 按索引排序流式合并（不占内存）
        with open(save_path, 'wb') as out:
            for i in range(total_chunks):
                with open(parts[i], 'rb') as f:
                    shutil.copyfileobj(f, out)
        # 5) 清理临时目录
        shutil.rmtree(cdir, ignore_errors=True)
        self.logger.info(f"分片合并完成: {saved_name} ({total_size} bytes, {total_chunks} 块)")
        return jsonify({'status': 'success', 'msg': f'上传成功: {saved_name}'}), 200

    @permission_required("user")
    def abort_upload(self):
        """取消分片上传，清理该 file_id 的全部临时分片。"""
        file_id = request.form.get('file_id', '')
        if not file_id:
            return jsonify({'status': 'error', 'msg': '缺少 file_id'}), 400
        try:
            cdir = self._chunk_dir(file_id)
        except ValueError:
            return jsonify({'status': 'error', 'msg': 'file_id 无效'}), 400
        if os.path.isdir(cdir):
            shutil.rmtree(cdir, ignore_errors=True)
        self.logger.info(f"分片上传已取消: {file_id}")
        return jsonify({'status': 'ok', 'msg': '已取消'}), 200

    def clean_expired_chunks(self):
        """清理超时未合并的临时分片（定时任务 30min + /upload/status 兜底）。"""
        if not os.path.isdir(self.chunk_folder):
            return 0
        now = time.time()
        expired = self.chunk_expire_minutes * 60
        cleaned = 0
        for fid in os.listdir(self.chunk_folder):
            cdir = os.path.join(self.chunk_folder, fid)
            if not os.path.isdir(cdir):
                continue
            meta = self._read_chunk_meta(cdir)
            last_ts = meta.get('last_active') or os.path.getmtime(cdir)
            if now - last_ts > expired:
                shutil.rmtree(cdir, ignore_errors=True)
                cleaned += 1
                self.logger.info(f"清理过期分片: {fid}")
        if cleaned:
            self.logger.info(f"过期分片清理完成，共删除 {cleaned} 组")
        return cleaned

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------
    @permission_required("public")
    def download_file(self, filename):
        # 下载前检查文件是否过期
        safe_name = self.sanitize_filename(filename)
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
        safe_name = self.sanitize_filename(filename)
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
            safe_name = self.sanitize_filename(filename)
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
                safe_name = self.sanitize_filename(filename)
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
                # 用 os.startfile（非扫描器高风险）替代 subprocess，达成严格模式（enforce）合规
                os.startfile(upload_path)
            else:
                return {
                    "code": 400,
                    "message": f"当前平台 {system} 暂不支持服务端打开文件夹"
                                f"（严格模式合规仅保留 Windows os.startfile）",
                    "data": {"path": upload_path, "system": system}
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
