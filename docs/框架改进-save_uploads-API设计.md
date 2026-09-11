# BasePlugin.save_uploads —— 同步持久化上传助手（API 设计 + 测试点）

> 状态：设计稿，供 FlaskToolkit 主项目照做实现。源起 AirDrop 插件化期间发现
> `save_uploaded_file` 仅覆盖"异步-临时目录"场景，缺"同步直接落盘持久化上传目录"的通用助手。
> 关联协调清单：wiki/projects/FlaskToolkit-AirDrop改造-框架协调清单 §6.1。

---

## 1. 目标

为"文件共享/持久化上传目录"类插件提供**一次调用**的同步上传助手，把当前各插件手写的
"净化文件名 + 重名去重 + 单文件大小预检 + 存储配额预检 + 直接落盘"收敛为框架内置能力，
同时兼容单文件与多文件、支持严格模式（enforce）能力一致性。

## 2. API 签名

```python
def save_uploads(
    self,
    file_key: str = 'files',
    dest_dir: str = None,
    *,
    dedup: bool = True,
    sanitize: bool = True,
    max_upload_mb: int = None,
) -> List[Dict[str, Any]]:
```

- 新增可选类属性 `upload_dir: str = None`（持久化上传目录，save_uploads 的默认落盘目标）。
- `save_uploaded_file` **保持不变**（异步/临时目录场景仍有价值，不破坏兼容）。
- 返回**每文件独立结果列表**（部分成功语义），不整体抛异常。

### 2.1 参数语义

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `file_key` | str | `'files'` | `request.files` 中的字段名；按 `getlist` 读取，天然支持单/多文件 |
| `dest_dir` | str/None | `None` | 落盘目录（绝对或相对 BASE_DIR）。`None` 时回退 `self.upload_dir`；两者皆空抛 `ValueError` |
| `dedup` | bool | `True` | 重名自动加序号 `name_1.ext`、`name_2.ext`…；`False` 时同名直接覆盖 |
| `sanitize` | bool | `True` | 文件名净化（统一策略）；`False` 用原始名（插件自担路径穿越风险，不推荐） |
| `max_upload_mb` | int/None | `None` | 单文件大小上限（MB）。`None` 沿用 route 级 max_upload > 插件 max_upload_size > 全局默认 的既有解析链 |

### 2.2 返回结构（每文件一个 dict）

```python
{
    'status':       'saved' | 'rejected',
    'original_name': str,          # 净化前的原始文件名
    'saved_name':    str,          # 实际落盘文件名（净化 + 去重后；rejected 为净化名或原始名）
    'path':          str,          # 已保存文件路径（仅 status='saved' 有效，否则 None）
    'size_bytes':    int,          # 落盘字节数（rejected 为 0）
    'reason':        str | None,   # rejected 原因：size_exceeded / quota_exceeded / invalid_type / empty
    'limit_mb':      float | None, # 拒绝相关：单文件上限 或 存储配额上限（size_exceeded / quota_exceeded）
    'remaining_mb':  float | None, # 配额拒绝时剩余可用空间
}
```

### 2.3 抛错（ValueError）

- `file_key` 不在 `request.files`：`ValueError('缺少上传文件')`（对齐 `save_uploaded_file`）。
- 键存在但 `getlist` 无任何条目：`ValueError('未选择文件')`。
- `dest_dir` 与 `self.upload_dir` 均为空：`ValueError('未指定上传目录：需传 dest_dir 或设置 upload_dir')`。
- 列表内**个别**空文件名条目：**跳过**（不抛错），其余照常处理。

## 3. 内部流程（逐文件，多文件循环）

```python
files = request.files.getlist(file_key)
if not files:
    raise ValueError('未选择文件')

dest = os.path.abspath(dest_dir or self.upload_dir)
if not dest_dir and not getattr(self, 'upload_dir', None):
    raise ValueError('未指定上传目录…')
os.makedirs(dest, exist_ok=True)

results = []
for file in files:
    # 1) 空文件名条目跳过
    if not file or not file.filename:
        continue
    original = file.filename

    # 2) 单文件大小预检（流 seek/tell，不落盘；同时取 f_size 供配额复用）
    oversize = self.check_upload_limit(file, max_upload_mb)   # 返回 0 或超限字节
    if oversize:
        results.append(rejected(original, 'size_exceeded',
                                limit_mb=self._resolve_upload_limit_mb(max_upload_mb)))
        continue

    # 3) 存储配额预检（复用 step2 的 f_size，避免二次 seek）
    f_size = _current_size(file)                              # 由 check_upload_limit 后流已复位，直接取
    quota = self.check_upload(f_size)                          # 未配置配额返回 ok=True
    if not quota['ok']:
        results.append(rejected(original, 'quota_exceeded',
                                limit_mb=quota.get('limit_mb'),
                                remaining_mb=quota.get('remaining_mb')))
        continue

    # 4) 类型校验
    ext = os.path.splitext(original)[1].lower()
    if self.allowed_upload_types and ext not in self.allowed_upload_types:
        results.append(rejected(original, 'invalid_type'))
        continue

    # 5) 文件名净化
    saved_name = self.sanitize_filename(original) if sanitize else original

    # 6) 去重 / 覆盖
    save_path = os.path.join(dest, saved_name)
    if dedup:
        base, ext = os.path.splitext(saved_name)
        n = 1
        while os.path.exists(save_path):
            saved_name = f'{base}_{n}{ext}'
            save_path = os.path.join(dest, saved_name)
            n += 1

    # 7) 落盘
    file.save(save_path)
    results.append({'status': 'saved', 'original_name': original,
                    'saved_name': saved_name, 'path': save_path,
                    'size_bytes': os.path.getsize(save_path),
                    'reason': None, 'limit_mb': None, 'remaining_mb': None})
return results
```

> 配额多文件语义：`check_upload` 的 usage 基于磁盘实时用量，循环中已落盘文件会自然计入后续文件的配额判断，语义正确，无需额外累加。

## 4. 配套：统一 `sanitize_filename`（6.2）

- 新增 `BasePlugin.sanitize_filename(filename) -> str`，默认实现为**增强策略**（比现有
  `core.utils.secure_filename_cn` 更严），保证路径穿越安全：
  1. 删除危险字符 `\ / : * ? " < > | \n \r \t`（保留中文、字母、数字、`-`、`_`、`.`）；
  2. 移除 `..` 序列，并 `lstrip('.')` 去掉前导点；
  3. 空名兜底 `未命名文件_<epoch>`。
- 建议框架内部 `core.utils.secure_filename_cn` 保留（兼容存量），`sanitize_filename` 可
  复用或独立，两者策略差异需在文档注明。
- AirDrop 现有手写 `get_safe_filename` 迁移为调用 `self.sanitize_filename`。

## 5. 严格模式（enforce）联动

`save_uploads` 会写 `dest_dir`。插件须在 plugin.json `capabilities` 声明
`filesystem:write:<dest_dir 相对 BASE_DIR 路径>`，否则 enforce 扫描报"未声明行为"。
设计文档与 Dev-Guide 需提示：dest_dir 用相对路径、与 capabilities 声明一致（AirDrop 用相对 `uploads` 即此约定）。

## 6. 测试点清单

### 6.1 单元测试（`tests/test_plugin_uploads.py`，test_client + 临时插件/模拟 request）

| # | 场景 | 断言 |
|---|------|------|
| T1 | 单文件保存 | `len==1`，`status='saved'`，`saved_name==净化名`，`path` 存在且内容一致 |
| T2 | 多文件 getlist | 全部 `saved`，返回条数==N，顺序稳定 |
| T3 | 单文件大小超限 | 该文件 `status='rejected'`、`reason='size_exceeded'`、`limit_mb` 正确，**dest_dir 未落盘** |
| T4a | 存储配额超限（已声明 storage:limit） | `reason='quota_exceeded'`，`remaining_mb>=0` |
| T4b | 未配置配额 | 不拒绝，全部 `saved`（`check_upload` 返回 ok=True） |
| T5a | 净化：`../../etc/passwd`、`a/b.txt`、`<bad>\|*.txt` | 无路径穿越，`saved_name` 无危险字符 |
| T5b | 中文文件名 | `saved_name` 保留中文 |
| T5c | `sanitize=False` | `saved_name==原始名` |
| T6a | 同请求两个同名 | `name.ext` + `name_1.ext` |
| T6b | 跨请求同名 | 加序号不覆盖 |
| T6c | `dedup=False` 同名 | 覆盖，内容为后者 |
| T7 | `allowed_upload_types=['.txt']` 传 `.exe` | `reason='invalid_type'` |
| T8a | file_key 缺失 | 抛 `ValueError` |
| T8b | 键存在但空 getlist | 抛 `ValueError('未选择文件')` |
| T8c | 个别空 filename | 跳过，其余正常，条数==正常数 |
| T9 | 部分成功（一超限一正常） | 返回 2 条，1 `rejected` 1 `saved`，正常文件已落盘 |
| T10a | dest_dir 不存在 | 自动创建 |
| T10b | dest_dir 与 upload_dir 皆空 | 抛 `ValueError` |
| T11 | 严格模式联动 | 插件 `save_uploads` 写未声明目录 → `scan.py` 报未声明 `filesystem:write` |

### 6.2 集成/端到端（复用 AirDrop runtime 冒烟）

| # | 场景 | 断言 |
|---|------|------|
| E1 | AirDrop 用 `save_uploads` 重构 `upload_files` | 上传→`/api/airdrop/files` 列表→下载内容一致（与手写实现回归等价） |
| E2 | enforce 加载 + 冒烟 | 复用 `tools/smoke_v110.py` 断言路径全部通过 |
| E3 | 中文名/重名经前端上传下载闭环 | 浏览器端到端：文件名正确、无乱码、重名加序号 |

## 7. 落地 checklist（主项目）

- [ ] `plugins/base_plugin.py`：新增 `upload_dir` 类属性、`save_uploads`、`sanitize_filename`
- [ ] 内部复用 `_resolve_upload_limit_mb` / `check_upload_limit` / `check_upload` / `allowed_upload_types`
- [ ] `tests/test_plugin_uploads.py`：T1–T11 全绿
- [ ] Dev-Guide 5.4.2：新增 `save_uploads`/`sanitize_filename` 表项 + enforce 能力一致性说明
- [ ] README 能力矩阵：文件传输能力新增同步落盘助手
- [ ] AirDrop：`get_safe_filename`→`self.sanitize_filename`，`upload_files`→`save_uploads`，回归 T+E
- [ ] 全量框架回归 + GitHub Actions
