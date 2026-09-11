# AirDrop 插件化改造方案

> 目标框架：FlaskToolkit（v4.x）　｜　源项目：AirDrop（simple-airdrop.py，532 行单文件 Flask 应用）
> 参考案例：Kaleido 插件化改造（3600 行 → 3 插件）

---

## 1. 背景与目标

AirDrop 是**局域网文件共享工具**：单文件 `simple-airdrop.py`（532 行），无类结构、无鉴权、绑定 0.0.0.0 直连，已实际运行（`uploads/` 有真实用户文件）。

**目标**：插件化迁移到 FlaskToolkit 框架，纳入框架统一能力体系：
- 插件生命周期（安装/更新/卸载/启停/热重载）
- 统一路由注册（`/api/airdrop/...` API + `/plugin/airdrop` 页面）
- 统一权限体系（可配置双模式鉴权）
- 配置持久化（`plugins/configs/airdrop.json`）
- 定时任务（过期文件清理 → `scheduled_tasks`）
- 复用框架日志、统计、静态资源分发

**已决策（2026-08-25）**：
1. **单插件 `airdrop`** —— 文件共享 + 网络地址 + 系统辅助属同一功能域，保持内聚。
2. **可配置双模式鉴权** —— 默认免登录即开即用；`configs/airdrop.json` 留 `auth_required` 开关，开启后按权限矩阵执行。

---

## 2. 现状分析

### 2.1 AirDrop 项目画像

| 维度 | 现状 |
|---|---|
| 代码 | `simple-airdrop.py` 单文件，无类，纯 Flask 视图函数 |
| 配置 | `config.json`：log（级别/大小轮转 backupCount=3）+ server（端口 9000、上传目录、有效期 86400s、大小上限 1GB、JSON_AS_ASCII、debug） |
| 前端 | `templates/index.html`（文件列表 + 上传/下载/删除/批量操作 + 局域网访问二维码）+ `static/qrcode.min.js` |
| 数据 | `uploads/` 已有 8 个真实文件（**必须零迁移风险保留**） |
| 日志 | `logs/server.log` RotatingFileHandler（10MB×3） |
| 鉴权 | 无，局域网直连 |

### 2.2 后端能力与路由全清单（9 个接口 + 首页）

| # | 路由 | 方法 | 功能 |
|---|---|---|---|
| 1 | `/` | GET | 首页：文件列表 + 局域网地址 + 配置注入模板 |
| 2 | `/api/network-addresses` | GET | 局域网 IP 列表（ipconfig / hostname -I 兜底 UDP） |
| 3 | `/upload` | POST | 多文件上传，重名自动加序号 |
| 4 | `/download/<filename>` | GET | 下载（含过期兜底校验） |
| 5 | `/delete/<filename>` | POST | 删除单文件 |
| 6 | `/batch-delete` | POST | 批量删除（JSON filenames） |
| 7 | `/batch-download` | POST | 批量下载打 zip（UTF-8 中文名兼容） |
| 8 | `/api/files` | GET | 文件列表 JSON（前端局部刷新） |
| 9 | `/api/test` | GET | 工具存活自检 |
| 10 | `/api/open-upload-folder` | GET | 服务端 explorer/文件管理器打开上传目录 |

### 2.3 关键实现点（迁移必须原样保留）

- `get_safe_filename`：路径穿越防护（过滤 `\ / : * ? " < > |` + 移除 `..` + 空名兜底）
- zip 批量下载中文名：`zinfo.flag_bits |= 0x800`（UTF-8）+ `create_system = 3`
- 下载响应头中文文件名：`Content-Disposition: filename*=UTF-8''...`
- 重名自动加序号（`name_1.ext`）
- 过期清理**双触发**：`/` 与 `/api/files` 请求时清理 + 下载时兜底校验删除
- 上传目录自动创建（`os.makedirs(exist_ok=True)`）

### 2.4 FlaskToolkit 框架能力（改造依赖点）

| 能力 | 说明 | 对应用法 |
|---|---|---|
| 插件基类 | `BasePlugin`：name/title/version/category/description/permission/routes | 子类继承 |
| 路由声明 | `routes` 属性，API 自动映射 `/api/<name>/<path>`；`page:True` 映射 `/plugin/<name>/<sub>` | 逐接口迁移 |
| 权限标记 | `@permission("public"/"user"/"admin")` | 见 3.3 |
| 模板命名空间 | `templates/plugins/<name>/` + `self.render('index.html')` | 前端迁移 |
| 静态资源 | `templates/plugins/static/<name>/` → `/plugin-static/<name>/<file>` | qrcode 迁移 |
| 配置持久化 | `plugins/configs/<name>.json` + `self.config` / `load_config` / `save_config` | 配置迁移 |
| 定时任务 | `scheduled_tasks` 属性（APScheduler） | 过期清理 |
| 鉴权可选 | `_check_permission`：**auth 插件不存在 → 全部放行** | 双模式鉴权基础 |
| 插件描述 | `plugins/<name>.json`（name/version/title/author/category/description/permission/require_framework_version） | 注册信息 |
| 生命周期钩子 | `on_load` / `on_shutdown` / `on_unload` / `on_uninstall` | 启动校验/清理 |

---

## 3. 改造总体设计

### 3.1 插件拆分（已决策：单插件）

```
plugins/
├── airdrop.py          # AirDropPlugin(BasePlugin)：全部后端能力
├── airdrop.json        # 插件描述（name/version/title/category/permission...）
├── configs/
│   └── airdrop.json    # 运行配置（upload_folder/expire_seconds/max_gb/auth_required）
templates/plugins/
├── airdrop/
│   └── index.html      # 主页面（原 templates/index.html 迁移）
└── static/
    └── airdrop/
        └── qrcode.min.js  # 静态资源（/plugin-static/airdrop/qrcode.min.js）
```

### 3.2 数据目录与配置策略

- **`uploads/` 保留原目录**：插件 `configs/airdrop.json` 配 `upload_folder` 指向**绝对路径**（`H:\Coding\Projects\AirDrop\uploads`），零迁移风险（对齐 Kaleido qbanks 策略）。
- 原 `config.json` 迁移映射：

| 原配置项 | 去向 |
|---|---|
| `server.port`（9000） | 废弃，改由框架管理（`FLASKTOOLKIT_PORT` / user_config / 自动探测） |
| `server.upload_folder` | `configs/airdrop.json` → `upload_folder` |
| `server.expire_seconds` | `configs/airdrop.json` → `expire_seconds` |
| `server.max_content_length_gb` | `configs/airdrop.json` → `max_gb`（见风险 5.1） |
| `server.debuggable` | 废弃，改由框架 `FLASKTOOLKIT_DEBUG` 管理 |
| `log.*` | 废弃，复用框架日志体系（插件自带 `self.logger`） |

### 3.3 鉴权策略（已决策：可配置双模式）

**原理**：框架 `_check_permission` 天然支持"鉴权可选"——`"auth" not in global_var.plugins` 时**所有接口直接放行**；auth 插件存在时按路由的 `@permission` 标记执行（含登录态 + CSRF + 角色）。

**落地方式（已实现，真开关）**：
- **路由按真实权限矩阵标记**（而非全部 public）：
  - 读操作（列表/下载/批量下载/网络地址/test/首页）→ `public`
  - 上传 → `user`
  - 删除/批量删除/打开上传文件夹 → `admin`
- **模式 A · 免登录（默认，auth_required=false）**：`_apply_auth_mode()` 在插件实例化时把全部路由方法 `_permission` 动态降级为 `public`，框架完全不拦截（即使已装 auth 插件）→ 即开即用。
- **模式 B · 鉴权（auth_required=true）**：恢复装饰器声明的权限矩阵，框架自动鉴权（登录/CSRF/403 页面）。
- **页面级联动**：`self.public_page = not auth_required`，供框架 `interceptor` 的 `/plugin/` 页面守卫豁免（见「实施发现」框架增强）。
- **启动一致性校验**：`on_load` 中若 `auth_required=true` 但未装 auth 插件，打 warning 告警（矩阵不会生效）。

> 关键实现：`_ORIG_PERM` 类级缓存装饰器声明的原始权限，避免 reload 切换模式时 `public` 永久污染 `__func__._permission`。

### 3.4 API 映射表（旧 → 新）

| 旧路由 | 新路由 | 方法 | 权限 |
|---|---|---|---|
| `/` | `/plugin/airdrop` | GET | public（页面） |
| `/api/network-addresses` | `/api/airdrop/network-addresses` | GET | public |
| `/upload` | `/api/airdrop/upload` | POST | user |
| `/download/<filename>` | `/api/airdrop/download/<filename>` | GET | public |
| `/delete/<filename>` | `/api/airdrop/delete/<filename>` | POST | admin |
| `/batch-delete` | `/api/airdrop/batch-delete` | POST | admin |
| `/batch-download` | `/api/airdrop/batch-download` | POST | public |
| `/api/files` | `/api/airdrop/files` | GET | public |
| `/api/test` | `/api/airdrop/test` | GET | public |
| `/api/open-upload-folder` | `/api/airdrop/open-upload-folder` | GET | admin |

---

## 4. 分阶段实施计划

### Phase 0 · 环境与备份 ✅ 已完成
- [x] 备份：`simple-airdrop.py`、`config.json`、`templates/`、`static/` → `backup/`
- [x] 确认框架版本（FRAMEWORK_VERSION=4.2.0）、依赖（Flask 3.1.3/APScheduler 3.11.3）
- [x] 记录 `uploads/` 现状（9 个文件）→ `backup/uploads-baseline.txt`

### Phase 1 · 后端插件 `airdrop` ✅ 已完成（冒烟 22/22）
- [x] 新建 `plugins/airdrop.py`：`AirDropPlugin(BasePlugin)`
  - 类属性：name=`airdrop`、title=`AirDrop 局域网文件共享`、category、version、permission=`user`
  - `__init__`：从 `self.config` 读 `upload_folder`/`expire_seconds`/`max_gb`/`auth_required`，`os.makedirs` 上传目录
  - `routes` 声明 10 个接口（见 3.4），路径参数 `<filename>`
- [x] 新建 `plugins/airdrop.json`（插件描述）与 `plugins/configs/airdrop.json`（运行配置，upload_folder 指向绝对路径）
- [x] 迁移核心逻辑：`get_safe_filename`、上传（重名加序号+双层大小校验）、下载（过期兜底）、删除、批量删除、批量下载 zip（UTF-8 兼容）、文件列表、网络地址、打开上传文件夹
- [x] 过期清理改造：`clean_expired_files()` → `scheduled_tasks` 定时任务（30min）+ 保留 `/api/airdrop/files` 请求时清理兜底
- [x] 日志改用 `self.logger`；跨接口共用逻辑抽为 `_list_files()`/`get_lan_addresses()`
- [x] 冒烟：9 条路由逐一验证（含上传/下载/zip 批量/中文名/重名加序号/权限矩阵）

### Phase 2 · 前端页面迁移 ✅ 已完成（冒烟 30/30 + 浏览器端到端）
- [x] `templates/index.html` → `templates/plugins/airdrop/index.html`，上下文改由 `render_index()` 注入
- [x] `qrcode.min.js` → `templates/plugins/static/airdrop/`，引用改为 `/plugin-static/airdrop/qrcode.min.js`
- [x] JS 内 9 处 API 路径改写（upload/download/delete/batch-*/files/open-upload-folder）
- [x] 二维码/访问地址端口改为 JS 动态取 `location.port`（适配框架动态端口，不再硬编码 9000）
- [x] 前端注入 `csrfHeaders()` helper，鉴权模式下 POST 自动带 X-CSRF-Token
- [x] 浏览器端到端：页面渲染 ✓、9 文件列出 ✓、二维码动态端口 ✓、上传→列表→下载内容一致 ✓

### Phase 3 · 权限矩阵与集成验证 ✅ 已完成（双模式 35/35）
- [x] 权限矩阵核对（public/user/admin 与 3.4 表一致）
- [x] 双模式验证：
  - 免登录（auth_required=false）：全部 API + 页面 + 静态资源未登录可访问
  - 鉴权（auth_required=true reload）：游客 public 200 / user·admin 401 / 登录 admin 后全功能 200
- [x] 框架回归：test_permission 20/20、test_page_router 21/21、test_stage2 19/19（interceptor 增强无破坏）
- [ ] 大文件上传（1GB 上限）与过期清理定时任务触发 —— 待真实环境（沙箱拦截 os.remove，见 5.4）
- [ ] 框架热重载：修改插件代码后 `/api/reload` 生效

### Phase 4 · 文档与收尾 ✅ 已完成
- [x] 方案文档更新（完成状态 + 实施发现 + 框架协调清单）
- [x] 框架文档同步：开发规范 v4.2 补充说明（public_page / plugin_common.js 修复 / AirDrop 插件）
- [x] plugin_common.js 复核与应用（见 7.5）
- [x] 框架发现总结为协调清单记忆，供主项目协调修改与 CI commit

---

## 5. 风险与注意事项

1. **1GB 上传上限**：原 `MAX_CONTENT_LENGTH=1GB` 是 app 级全局配置；FlaskToolkit 默认 `MAX_CONTENT_LENGTH` 可能远小于此。需确认框架全局限制，必要时在框架 user_config 提升或插件文档说明（大文件传输对局域网工具是核心诉求）。
2. **端口变更**：9000 → 框架动态端口（自动探测），前端二维码地址必须由页面上下文注入实际端口，不能写死。
3. **`open-upload-folder` 的 subprocess**：服务端操作系统调用，权限必须 `admin`，防未授权触发。
4. **沙箱文件保护**：删除类接口测试在沙箱可能被拦截（Kaleido 踩坑），需真实环境或 bash 验证。
5. **`uploads/` 绝对路径硬编码**：插件 config 指向 AirDrop 原目录，跨机迁移时需改配置；文档需说明。
6. **路径穿越与中文名防护**：迁移时严禁简化，逐行保留。
7. **双模式边界**：`auth_required=true` 但未装 auth → on_load 告警；避免"以为鉴权实际裸奔"的误判。

---

## 6. 待办事项清单（本轮不做 / 可选）

- [ ] 前端工具包化（`config.json + html + static` zip 分发，纯前端形态）——AirDrop 依赖后端 API，非纯前端工具，优先级低
- [ ] 上传文件类型/大小前端友好提示
- [ ] 文件分享短链 / 密码保护等增强功能（超出"插件化迁移"范畴）
- [ ] 审计日志对接框架（框架已有 P2-2 audit 能力，可选接入）

---

## 附：关键源码定位（迁移参考）

| 原代码 | 位置 |
|---|---|
| 配置加载 | `simple-airdrop.py:47 load_config()` |
| 网络地址 | `simple-airdrop.py:177 get_lan_addresses()` / `236` |
| 安全文件名 | `simple-airdrop.py:242 get_safe_filename()` |
| 过期清理 | `simple-airdrop.py:253 clean_expired_files()` |
| 上传 | `simple-airdrop.py:302 upload_files()` |
| 下载 | `simple-airdrop.py:335 download_file()` |
| 批量下载 zip | `simple-airdrop.py:388 batch_download()` |
| 打开上传文件夹 | `simple-airdrop.py:453 open_upload_folder()` |

框架参考：`plugins/base_plugin.py`（BasePlugin）、`routes/plugin.py`（路由分发）、`core/permission.py`（权限）、`core/plugin_loader.py`（加载包装）。

---

## 7. 实施发现（AirDrop 改造过程中的框架观察/优化与验证结论）

### 7.1 框架增强：`/plugin/` 页面登录守卫支持插件级豁免（已实现于 `routes/interceptor.py`）

**现象**：`global_auth_interceptor` 的 `LOGIN_GUARD_PREFIXES = ['/plugin/']` 把**所有插件页面**强制要求登录，且独立于插件自身权限标记——即使插件 API 全部 public（如 AirDrop 免登录模式），其 `/plugin/<name>` 页面仍被重定向到登录页。

**影响面**：对「局域网公开工具 / 信息落地页」类插件不友好，无法实现整插件公开。

**增强**：插件实例声明 `public_page=True` 时，其 `/plugin/` 页面豁免登录守卫（默认 False，Kaleido 等现有插件行为不变）。AirDrop 在 `__init__` 中 `self.public_page = not self.auth_required`，与免登录模式联动。

**验证**：框架回归 test_page_router 21/21、test_permission 20/20、test_stage2 19/19 全通过。

### 7.2 框架观察点：无全局 `MAX_CONTENT_LENGTH` 上传限制

框架未设置全局 `MAX_CONTENT_LENGTH`（Flask 默认无限制），任何插件上传可无限大。AirDrop 被迫在插件内实现**双层校验**（Content-Length 预检 + 保存后实测，超限删除拒绝）。

**建议**：框架提供统一上传大小限制机制（全局配置 + 插件级覆盖），避免各插件重复实现。

### 7.3 沙箱验证结论（环境限制，非插件缺陷）

- 冒烟/端到端中 `os.remove` 被沙箱保护拦截（返回 500 + 友好错误信息），**删除类功能需真实环境复核**；插件对 `OSError` 的捕获与友好报错逻辑验证正确。
- 上传/下载/批量下载/列表/重名加序号/中文名/权限矩阵/页面渲染/二维码动态端口 全部经 test_client 与浏览器端到端验证通过。

### 7.4 其他优化：二维码/访问地址端口动态化

原实现硬编码端口 9000，框架端口为动态探测。迁移后页面 JS 改为 `location.port` 动态取值，彻底解耦端口，扫码地址始终正确。

### 7.5 plugin_common.js 复核与应用（2026-08-26）

**应用**：AirDrop 前端引入框架 `/static/js/plugin_common.js`（全局 XHR/fetch 拦截统一注入 `X-CSRF-Token` + 统一处理 401/403 + 登录态校验），移除自研 `csrfHeaders` 手动注入。免登录模式下无 csrf cookie 自动跳过校验，不误跳登录。

**复核发现并修复 bug**：`PluginCommon.request()` 与全局 XHR `send` 拦截对非安全方法**双重注入 X-CSRF-Token**，同名头被浏览器逗号拼接为 `token, token`，鉴权模式下写请求后端 CSRF 双提交校验 403。验证链路：echo 路由回显确认多值 → 鉴权模式 admin 接口实测 403 → 移除 request() 手动注入（依赖全局拦截单次）→ echo 单值通过、delete 业务 404 通过、鉴权模式上传 200。原生 fetch/XHR 不受影响（拦截器单次注入）。

**注意**：`PluginCommon.request()` 与全局拦截在**同一文件**（plugin_common.js），引入即生效；使用 `request()` 的插件在本次修复后不再踩双重注入。

### 7.6 框架协调清单（供主项目）

AirDrop 改造对框架的全部改动点、发现的框架问题与待办已沉淀为记忆 [FlaskToolkit-AirDrop改造-框架协调清单](memory://wiki/projects/FlaskToolkit-AirDrop改造-框架协调清单)，供主项目协调修改与 GitHub Actions commit。
