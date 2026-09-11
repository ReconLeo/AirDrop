# AirDrop · 轻量级局域网文件传输

对标苹果 AirDrop 的**零依赖、轻量化局域网文件传输工具**：无需公网服务器、无需客户端，仅通过浏览器即可实现同一局域网下多设备快速传文件。适合手机热点、企业内网、家庭 WiFi 等场景。

本仓库同时提供**两种形态**：

- **独立应用**（`standalone/`）：基于 Flask 的单文件应用 `simple-airdrop.py`，零第三方依赖，即下即用。
- **FlaskToolkit 插件**（`plugin/`）：将 AirDrop 插件化迁移至 [FlaskToolkit](https://github.com/ReconLeo/FlaskToolkit) 框架，纳入其权限/生命周期/路由/配置等统一能力。插件落地文件被 FlaskToolkit 主仓库 `.gitignore` 排除，由本仓库独立承载分发。

---

## 特性

### 基础传输

- 单文件 / 多文件同时上传，单文件最大支持 1GB（可配置）
- 两种上传方式：拖拽上传 + 点击选择文件
- 实时上传进度条，上传完成自动刷新文件列表
- 单文件独立下载，下载前自动校验文件有效性
- 批量下载：多文件自动打包 ZIP，支持 4GB+ 大文件，全中文文件名无乱码（兼容 Win/Mac/Linux）

### 文件管理

- 手动删除（按钮 / 双击）、批量删除（二次确认防误删）
- 多模式选择：全选 / 反选 / 区间选择；PC 端 Shift 连续选择，移动端长按 +「范围选」
- 文件过期自动清理（默认 24 小时，可配置）
- 重名文件自动加序号后缀
- 文件列表 30s 自动静默刷新（可调/可关）+ 手动刷新 + 局部刷新保留选中态 + 新文件主动提示

### 多端适配

- 响应式布局，兼容 PC / 手机 / 平板
- 跨平台：所有设备只要有浏览器即可，支持 Win / Mac / Linux / Android / iOS
- 零蜂窝流量：所有数据仅在局域网内传输
- 移动端操作优化：大按钮、大勾选区域、长按震动反馈

### 安全防护

- 文件名安全过滤（防路径穿越）
- 文件大小限制（防磁盘占满）
- 全内网传输，无任何公网请求，数据不流出局域网
- 接口参数校验

---

## 项目结构

```
AirDrop/
├── standalone/                     # 独立应用（可独立运行）
│   ├── simple-airdrop.py           #   单文件 Flask 应用
│   ├── config.json                 #   运行配置（端口/上传目录/有效期/大小上限）
│   ├── templates/index.html        #   前端页面
│   ├── static/qrcode.min.js        #   二维码静态资源
│   └── favicon.ico / favicon.png
├── plugin/                         # FlaskToolkit 插件（部署到框架加载）
│   ├── airdrop.py                  #   插件主文件（AirDropPlugin(BasePlugin)）
│   ├── airdrop.json                #   插件描述
│   ├── configs/airdrop.json        #   插件运行配置（含可配置双模式鉴权 auth_required）
│   └── frontend/                   #   插件前端（部署到框架 templates/plugins/）
│       ├── index.html
│       └── static/qrcode.min.js
├── tests/                          # 插件加载回归测试（从 FlaskToolkit 移交）
│   └── test_airdrop_loader.py
└── docs/                           # 插件化改造方案 / 测试交接说明 / 遗留文档
    ├── AirDrop-插件化改造方案.md
    ├── FlaskToolkit-插件测试交接说明.md
    └── _legacy/
```

---

## 快速开始

### 方式一：独立应用（standalone）

```bash
cd standalone
python simple-airdrop.py
```

启动后访问 `http://127.0.0.1:5000`；局域网访问改为绑定 `0.0.0.0`（见 `simple-airdrop.py` 头部配置）。文件保存在 `standalone/uploads/`。

### 方式二：FlaskToolkit 插件

**推荐：发布包安装**（含完整性清单 + 可选签名）

1. 从 Release 下载 `airdrop-vX.Y.Z.zip`（或自行用 `python tools/package.py pack package/ -o dist/airdrop.zip --type backend --sign keys/airdrop_signing_private.pem` 打包）。
2. 在框架根目录离线安装：

```bash
python tools/install_plugin.py backend airdrop-vX.Y.Z.zip
```

3. 启动框架，访问 `/plugin/airdrop`。

> 签名验证（可选）：包已用本项目密钥对签名，自架设框架如配置 `PLUGIN_PUBLIC_KEY_PEM` 指向 `keys/airdrop_signing_public.pem`，安装时即真实验签；未配置公钥则仅做完整性校验（sha256），两种场景均可正常安装。

**手动部署**（源码形态）：

1. 准备 [FlaskToolkit](https://github.com/ReconLeo/FlaskToolkit) 框架（v4.x）。
2. 将 `plugin/` 下的文件按对应路径部署到框架：
   - `plugin/airdrop.py` → `plugins/airdrop.py`
   - `plugin/airdrop.json` → `plugins/airdrop.json`
   - `plugin/configs/airdrop.json` → `plugins/configs/airdrop.json`
   - `plugin/frontend/index.html` → `templates/plugins/airdrop/index.html`
   - `plugin/frontend/static/` → `templates/plugins/static/airdrop/`
3. 启动框架，访问 `/plugin/airdrop`。

> 分发源目录 `package/`（`plugin.json` + `airdrop.py` + `templates/` + `static/`）由 `plugin/` 工作源组装，是 `tools/package.py pack` 的输入；`dist/` 为打包构建产物，不入库。

> 双模式鉴权：插件 `configs/airdrop.json` 中 `auth_required=false`（默认）全部接口免登录即开即用；`true` 时按权限矩阵执行（读 public / 上传 user / 删除·打开文件夹 admin），需框架已装 `auth` 插件。

---

## 配置（独立应用 config.json）

| 配置项 | 默认值 | 说明 |
| ------ | ------ | ---- |
| `server.port` | 5000 | 服务监听端口 |
| `server.upload_folder` | `uploads` | 上传文件保存目录 |
| `server.expire_seconds` | 86400 | 文件过期时间（秒） |
| `server.max_content_length_gb` | 1 | 单文件最大上传大小（GB） |

插件形态的配置见 `plugin/configs/airdrop.json`（含 `upload_folder` / `expire_seconds` / `max_gb` / `auth_required`）。

---

## 测试

插件加载回归测试（需 FlaskToolkit 框架代码，设置 `FLASKTOOLKIT_ROOT` 指向框架仓库根）：

```bash
FLASKTOOLKIT_ROOT=<FlaskToolkit仓库根> python tests/test_airdrop_loader.py
```

详见 `docs/FlaskToolkit-插件测试交接说明.md`。

---

## 许可

[MIT](./LICENSE) © 2026 ReconLeo
