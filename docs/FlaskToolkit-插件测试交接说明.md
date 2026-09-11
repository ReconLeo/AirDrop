# FlaskToolkit 插件测试交接说明（AirDrop 子项目）

> 交接日期：2026-09-06
> 交接方：FlaskToolkit 主仓库（ReconLeo/FlaskToolkit）→ AirDrop 子项目（本目录）

## 1. 交接背景

AirDrop（局域网文件共享）以独立子项目开发，其插件落地文件（`plugins/airdrop.py`、`plugins/airdrop.json`、`templates/plugins/airdrop/`、`templates/plugins/static/airdrop/`）被 FlaskToolkit 仓库 `.gitignore` 有意排除，**不入主仓库**。

2026-09-06 在 FlaskToolkit v4.10.0 开发中发现 AirDrop 插件无法被框架加载的 bug：

- 现象：真实 `load_plugins()` 报 `TypeError: 'method' object is not iterable`
- 根因：`plugins/airdrop.py` 的 `def routes(self)` 未加 `@property`（基类 `plugins/base_plugin.py` 声明为 `@property @abstractmethod`），框架 `for route in plugin_instance.routes` 拿到 bound method
- 修复：`routes` 补 `@property`，并清理 `max_upload_size` 上重复的 `@property` 装饰器（`max_upload` 仍为属性）；真实 load_plugins 验证加载成功（9 路由）

## 2. 交接文件

| 文件 | 原位置 | 现位置 |
|------|--------|--------|
| `tests/test_airdrop_loader.py`（8 项插件加载回归） | FlaskToolkit 仓库 `tests/`（commit `38c6ea4` 入库，`2ecb8b7` 后移除） | 本子项目 `tests/test_airdrop_loader.py` |

FlaskToolkit 仓库内不再保留本测试（含相关文档注记已同步清理），AirDrop 插件加载回归由本子项目负责维护。

## 3. 运行前提与方法

本测试依赖 FlaskToolkit 框架代码（`app.py` / `core/plugin_loader.py` / `global_var.py` / `plugins/base_plugin.py` 骨架），因此在**框架仓库环境**中运行：

```bash
# 方式一（推荐）：设置 FLASKTOOLKIT_ROOT 指向框架仓库根（克隆一份 FlaskToolkit 到本地）
FLASKTOOLKIT_ROOT=C:/path/to/FlaskToolkit python tests/test_airdrop_loader.py

# 方式二：把本文件放回框架仓库 tests/ 目录直接运行（默认取文件上级目录为框架根）
python tests/test_airdrop_loader.py
```

前提条件：

1. FlaskToolkit 仓库已 clone 且依赖已装（`pip install -r requirements.txt`）
2. AirDrop 插件落地文件已复制到框架仓库：`plugins/airdrop.py`、`plugins/airdrop.json`、`templates/plugins/airdrop/`、`templates/plugins/static/airdrop/`
3. 插件缺失时测试会 **SKIP 并退出 0**（不误报失败）

## 4. 断言清单（8 项）

| 组 | 断言 | 验证点 |
|----|------|--------|
| A | A1 加载成功 / A2 routes 为可迭代属性 | routes @property 修复的核心验证 |
| B | B1 路由数 9 / B2 含核心路由 / B3 upload 带 route 级 max_upload / B4 含路径参数路由 | 路由结构（变更时需同步更新） |
| C | C1 `/plugin/airdrop` 索引页 200 | 页面路由可访问 |
| D | D1 `/api/airdrop/network-addresses` 200 | API 路由可访问（public） |

## 5. 维护职责（AirDrop 侧）

- **AirDrop 插件代码变更后**（尤其路由增删、`routes` 结构、`max_gb`/`max_upload` 语义），必须运行本回归，并同步更新 B 组断言（路由数量、核心路径集合）。
- **框架侧变更影响**：若 FlaskToolkit 基类接口（如 `routes` 装饰要求、插件加载方式）变更，可能导致本测试失败或 SKIP 逻辑失效，需随框架版本升级同步调整。

## 6. 历史追溯

- `38c6ea4`（FlaskToolkit）：test_airdrop_loader.py 首次入库（条件运行：airdrop.py 缺失时 SKIP）
- `2ecb8b7`（FlaskToolkit）：本测试从主仓库移除，移交至本子项目
