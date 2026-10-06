# XiangQiLens 优化评审报告

**日期**：2026-10-06
**基线**：`main` @ `309b2df`（v0.5.1，工作区干净）
**范围**：`D:\opencode\XiangQiLink` 全仓库（代码 / 构建 / 测试 / 发布 / 仓库卫生）
**方法**：AST 静态分析（自写脚本）+ 真实微基准（本机实测）+ `git` 取证。所有数字均来自本次实际运行，命令与行号可复核。

---

## 0. 结论速览

| # | 优先级 | 问题 | 位置 | 收益 | 风险 |
|---|---|---|---|---|---|
| 1 | **P0** | 静止画面仍每帧全量推理（pose+cls） | `app_vision.py:504-619` | 静止帧 ~195 ms → ~13 ms（含抓帧），GPU 推理在静止期完全停下 | 低 |
| 2 | **P0** | 抓帧有效性校验用 `astype(float32)` 全图 | `app_backend.py:357-358` | 每帧省 **23.7 ms**（683×1253 实测 24.0→0.32 ms） | 极低 |
| 3 | **P0** | `xq_vision.py` / `engine_client.py` **从未入库** | `xq_research/`（被 gitignore） | 消除"核心代码无版本历史/无备份"风险 | 无（只是 `git add`） |
| 4 | P1 | 无 `requirements.txt` / CI / lint / pytest | 仓库根 | 依赖可复现、23 个现成测试脚本可自动跑 | 低 |
| 5 | P1 | 自动走棋默认值：README 与代码注释互相矛盾 | `README.md:8-9` vs `:46/356/375/508`；`app_vision.py:2238/2245/2429/2450` | 消除"真点落子"这类安全相关行为的文档歧义 | 低 |
| 6 | P1 | 单文件上帝类：`Worker` 1338 行 / `MainWindow` 1162 行 | `app_vision.py:192-1530 / 1700-2862` | 可测试性、评审成本 | 中 |
| 7 | P2 | 开局库两套 SQLite 实现重复 ~80 行 | `app_book.py:250-327` vs `535-616` | 去重 | 低 |
| 8 | P2 | 1.1 GB 本地产物（`dist/` 673 MB + `_build_ok/` 454 MB） | 仓库根 | 磁盘 | 无 |
| 9 | P2 | 图标引用**另一个项目** `refs/chessboard` | `tools/build_exe.py:116` | 换机/换目录可构建 | 低 |
| 10 | P3 | `src/` 2048 行旁路实现 + 一次性脚本堆积 | `src/`、`tools/patch_*.py`、`extract_model{,2,3}.py` | 认知负担 | 低 |

---

## 1. 性能：有实测数据的三处

### 1.1【P0】静止画面仍在全量推理 —— 最大的一处浪费

**现状（证据）**：主循环 `app_vision.py:480-619` 的顺序是

```
480  while self._run:
504      bgr = self.source.grab()              # 抓帧
512      res = self._vision.infer(...)         # ★ 全量推理：pose + 90 格分类
536      check_board_geometry(...)
553      if self.anim_suppress:
554          moving, ratio = self._frame_moving(res["warped"])   # ★ 变化检测在推理之后
617      if self._same < self.confirm or key == self._published:
618          time.sleep(0.10); continue         # ★ 静止帧走这里 —— 但推理已经做完了
```

也就是说：**动画抑制闸门用的是推理输出（`res["warped"]`），而推理本身在闸门之前无条件执行**。
棋盘静止时（人思考、等对手），循环以 `推理 170ms + sleep 100ms` 的节拍空转 —— 按 README 实测
（`README.md:211-212`，DirectML：pose 4.1 ms + cls 128.0 ms，完整单帧 170 ms），静止期每帧
约 170 ms 的 GPU 推理被完全浪费。

**改法**：在 `grab()` 之后、`cvtColor`/`infer()` 之前，加一道**廉价预闸门**（复用已有的纯函数
`gray_small` / `motion_ratio`，见 `app_vision.py:71-87`）：

```python
# Worker.__init__ 增加
self._prev_raw_gray = None
self._idle_ticks = 0

# run() 循环内，bgr = self.source.grab() 成功之后、cvtColor 之前插入
raw_g = gray_small(bgr)                      # 实测 0.97 ms（683x1253）
prev, self._prev_raw_gray = self._prev_raw_gray, raw_g
if (prev is not None
        and not self._force_infer               # 点「分析」/「新局」/自动判执子时置 True
        and motion_ratio(prev, raw_g) <= self.motion_thresh):
    self._idle_ticks += 1
    time.sleep(0.03)                            # 让出 CPU，仍以 ~30Hz 轮询
    continue                                    # 跳过 cvtColor + infer
self._idle_ticks = 0
```

**必须保留的例外**（否则会卡死或行为回退）：
- `self._published` 为空（还没采信过任何局面）、`self.auto_side_pending` 为真、刚点过「分析/新局」→ 强制推理；
- 窗口状态异常 / 抓帧失败 / 识别异常的分支保持原样；
- 连续跳过的帧数设上限（如 200 帧强制放行一次），与既有「最长抑制时长」兜底（`suppress_timed_out`，`app_vision.py:97-105`）思路一致。

**实测收益**：

| 项 | 683×1253（典型棋局窗口） | 2560×1440（本次基准窗口） |
|---|---|---|
| 预闸门成本（灰度+缩放+差分） | **0.99 ms** | 1.17 ms |
| 现在静止帧成本（grab 校验 + cvtColor + 推理） | ≈ 24.0 + 0.5 + 132~170 = **157~195 ms** | ≈ 29.5 + 1.6 + 132~170 |
| 改后静止帧成本 | ≈ 0.99 ms + grab | ≈ 1.17 ms + grab |

即**静止帧的"计算+推理"部分从 ~195 ms 降到 ~1 ms 量级（约 196x）**，GPU 推理在静止期完全停下；
加上无法避免的抓帧（683×1253 约 11.8 ms，见 1.3），**静止帧整体约 195 ms → 13 ms（约 15x）**。
响应性无损失：棋盘一变，当帧立刻恢复推理。

> 补充：预闸门是对**整个窗口**做差分，若窗口里有秒表/聊天区动画，会退化成"每帧都推理"——
> 与今天行为一致，不会更差。想再进一步，可用上一次成功识别的棋盘 bbox（`res["warp_mat"]`
> 或关键点包围盒）把差分限制在棋盘区域内。

### 1.2【P0】抓帧校验的 24 ms

**现状（证据）**：`app_backend.py:355-360`

```python
355  img = self._print_window()
356  if img is not None:
357      a = img.astype(np.float32)                                  # ★ 全图转 float32
358      if float(a.std()) > 3.0 and float((a.max(axis=2) > 12).mean()) > 0.02:
```

一次 `astype(np.float32)` 在 683×1253×3 上分配 10 MB 临时数组，只为了判断"这张图不是全黑"。

**实测（本次运行，同一台机器）**：

| 实现 | 683×1253 | 2560×1440 |
|---|---|---|
| 现状 `astype(float32).std() + max(axis=2)` | **24.01 ms** | **29.54 ms** |
| 抽样版（`img[::8, ::8]`，uint8 上判活） | **0.32 ms** | **0.39 ms** |

**改法**（保持判据语义不变，~75x 加速）：

```python
sub = img[::8, ::8]                                  # 1/64 采样
if int(sub.max()) - int(sub.min()) < 8:              # 近似 std 判据
    img = None
else:
    live = float(np.count_nonzero(sub.max(axis=2) > 12)) / sub[:, :, 0].size
    if live > 0.02:
        self.stats["background"] += 1
        return img
```

对 683×1253 的窗口，这一项单独就省掉**每帧 ~23.7 ms ≈ 单帧预算的 12%**。

### 1.3【P2】`PrintWindow` 每帧重建 GDI 对象

**现状**：`app_backend.py:381-421` 每帧都 `GetWindowDC → CreateCompatibleDC → CreateCompatibleBitmap(w,h)
→ SelectObject → DeleteObject → DeleteDC → ReleaseDC`，即每帧新建/销毁一张 w×h×4 的位图
（683×1253 → 3.4 MB；2560×1440 → 14.7 MB）。

**实测**：2560×1440 窗口 `PrintWindow` 全流程 **50.8 ms/帧**（含 GDI 创建销毁）；按面积折算到
683×1253 约 **11.8 ms/帧** —— 与 README 的 170 ms 单帧扣掉推理后基本吻合，说明抓帧本身已占单帧 ~7%。

**改法**：把 `(hwnd_dc, mfc_dc, save_dc, bmp)` 缓存为实例属性，仅在 `GetWindowRect` 结果变化
或抓帧失败时重建，其余帧只 `PrintWindow + GetBitmapBits`。注意在 `finally` 里只在销毁时释放，
并处理窗口 resize 的失效路径。收益：省掉每帧一次 GDI 对象创建/销毁与一次 3.4 MB 位图分配。

### 1.4【P3】主循环的 `time.sleep` 轮询

`run()` 里有 10 处硬编码 sleep（`500/508/516/541/572/582/618/632/651/656`：0.05~1.0 s）。
在 1.1 落地后，这些 sleep 会变成"响应延迟"的主要来源（最坏多等 1.0 s）。
建议统一为一个 `self._tick(min_interval)` 小工具，并按分支语义命名（`_wait_idle()`、
`_wait_settle()`、`_wait_window_restore()`），便于日后调参与测试。

---

## 2. 资产与版本控制：一处高风险

### 2.1【P0】两个核心运行时模块从未进入版本控制

**取证**：

```
$ git log --all --oneline -- xq_vision.py engine_client.py
(空)
$ git ls-files | Select-String 'vision|engine_client'
app_vision.py          # 只是文件名里含 vision，不是模块
src/vision.py
tools/bench_vision.py
```

`app_backend.py:73-74` 把 `RESEARCH`（= `<应用根目录>/../xq_research`，`app_backend.py:46-69`）
插进 `sys.path`，然后：

- `app_backend.py:457` `from xq_vision import XiangqiVision` —— 识别模型封装（9.7 KB）
- `xq_research/engine_client.py` —— UCI 引擎客户端（6.4 KB，`UciEngine`）

这两个文件**只存在于 `D:\opencode\xq_research\`**，而该目录被 `.gitignore` 排除
（`xq_research/`），且它自己**不是 git 仓库**（`Test-Path .git` → False）。
仓库里目前有三份字节相同的副本，全部未被跟踪：

```
1623B423B6BAC083  D:\opencode\xq_research\xq_vision.py
1623B423B6BAC083  D:\opencode\XiangQiLink\dist\XiangQiLens\xq_research\xq_vision.py
1623B423B6BAC083  D:\opencode\XiangQiLink\_build_ok\xq_research\xq_vision.py
AC235403C9CD4ECD  D:\opencode\xq_research\engine_client.py
AC235403C9CD4ECD  D:\opencode\XiangQiLink\dist\XiangQiLens\xq_research\engine_client.py
```

**风险**：引擎协议解析、引擎进程生命周期、`prefer_cuda`/DirectML provider 选择这些**逻辑**
都在其中，却没有一行历史；模型权重与 nnue 不入库是合理的（体积+许可证），但**代码不该跟着一起丢**。

**改法**（不破坏现有布局，最小改动）：

```powershell
cd D:\opencode\XiangQiLink
# 把两份代码纳入仓库（保留 xq_research 作为模型/引擎资源的运行时目录）
New-Item -ItemType Directory -Force vendor\xq_research | Out-Null
Copy-Item D:\opencode\xq_research\xq_vision.py     vendor\xq_research\
Copy-Item D:\opencode\xq_research\engine_client.py vendor\xq_research\
# .gitignore 里对代码开白名单（资源仍排除）
Add-Content .gitignore "`n# 核心代码例外（资源仍不入库）`n!vendor/xq_research/*.py"
git add vendor/xq_research/xq_vision.py vendor/xq_research/engine_client.py .gitignore
```

`app_backend.py` 的 `RESEARCH` 查找顺序（`app_backend.py:57-69`）已支持多个候选路径，
把 `vendor/xq_research` 加为**第一候选**即可，源码运行与打包运行都不受影响。

### 2.2【P2】打包图标依赖另一个项目

`tools/build_exe.py:116`：

```python
icon = ROOT.parent / "refs" / "chessboard" / "server" / "icons" / "icon.ico"
```

即 `D:\opencode\refs\chessboard\...` —— 与 `XiangQiLink` 无依赖关系的另一个仓库。
换台机器、或 `refs/` 被清理后，打包会**静默丢失图标**（`if icon.is_file()` 不满足就跳过，不报错）。
生成的 `XiangQiLens.spec` 里同样写着绝对路径 `D:/opencode/XiangQiLink/app_vision.py`、
`pathex=['D:/opencode/xq_research']`（该 spec 被 `*.spec` 忽略，属有意为之）。

**改法**：把 icon 复制进仓库 `assets/icon.ico` 并提交，`build_exe.py` 改指 `ROOT / "assets" / "icon.ico"`，
且找不到时 `print("[失败]") + return False` 而不是静默跳过。

### 2.3【P2】1.1 GB 本地产物

| 目录 | 体积 | 状态 |
|---|---|---|
| `dist/` | 673.4 MB | 已 gitignore（含一份完整 `xq_research` 拷贝） |
| `_build_ok/` | 453.9 MB | 已 gitignore |
| `engines/` | 106.2 MB | 已 gitignore |
| `build/` | 46 MB | 已 gitignore |
| `analysis/` + `shots/` + `probe_out/` | 56.9 MB | 已 gitignore |

`git status --short` 干净、`git ls-files` 只有 88 个文件 —— **仓库本身是干净的**，这是本地磁盘占用问题。
可安全清理（保留一份当前发行目录即可）：

```powershell
cd D:\opencode\XiangQiLink
Remove-Item -Recurse -Force _build_ok, build, analysis, probe_out, shots -ErrorAction SilentlyContinue
# dist 保留最新一份；确认新包已发布后再删
```

---

## 3. 工程化基础设施：从 0 到 1

### 3.1【P1】项目定义文件一个都没有

`Test-Path` 实测结果（全部 `False`）：`requirements.txt`、`pyproject.toml`、`setup.cfg`、
`pytest.ini`、`conftest.py`、`tox.ini`、`.pre-commit-config.yaml`、`mypy.ini`、`ruff.toml`、`.github`。

依赖只写在 README 的 `pip install` 行里（`README.md:68-71`）：

```
PySide6 opencv-python numpy mss pywin32 cchess
onnxruntime-directml        # 或 onnxruntime（CPU）
```

**改法**：加 `requirements.txt`（锁到主版本）+ `requirements-dev.txt`（`pyinstaller pytest ruff`），
版本号单一来源已经有了（`app_backend.py:26 __version__ = "0.5.1"`，全仓库仅此一处硬编码版本），
再让它派生到 `pyproject.toml` 的 `version` 即可。

### 3.2【P1】25 个测试脚本，0 个 CI

`tools/test_*.py` 共 25 个，**扫描结果：23 个是可自动判定的**（用 `return 1` 或
`check(...)` 判定，最后 `sys.exit(main())`），合计 **39 处 `return 1` + 243 处 `check()` 调用**；
只有 2 个是纯看输出的人工脚本（`test_align.py`、`test_model.py`）。

抽样核对（`tools/test_guard.py`）确认判定是真的、不是摆设：

```python
58  if t.moves != ["h2e2", "h9g7", "b0c2"]:
59      print(f"    [失败] 着法序列不对: {t.moves}")
60      return 1
...
175 if __name__ == "__main__":
176     sys.exit(main())
```

**改造分档**（按是否真的访问系统重新核对过一遍；判据：文件里有无 `win32gui.*` /
`PrintWindow` / `GetWindowRect` / `mss.*` 这类**运行时**硬调用）：

| 档 | 数量 | 脚本 | 说明 |
|---|---|---|---|
| **A：CI 直接可跑** | 18 | `test_guard` `test_position` `test_geometry` `test_flip` `test_legality` `test_settle_gate` `test_book` `test_book_codec` `test_turn` `test_wdl` `test_winrate` `test_winstate` `test_window_score` `test_window_filter` `test_newgame` `test_arrow` `test_automove_gates` `test_align` | 纯逻辑，或只把 `hwnd` 当参数传递、不实际访问窗口；`test_automove_gates` 已能"无可用窗口时明确跳过而非报失败"（`309b2df`） |
| **B：需要模型/引擎资源** | 4 | `test_model` `test_e2e` `test_live_chain` `test_screen_region_guard` | 依赖 `xq_research` 下的 ONNX/引擎，可作 nightly；`test_model` 是纯输出脚本、无自动判定（见下） |
| **C：需要真实窗口/屏幕，保持手工** | 3 | `test_autoclick` `test_automove_e2e` `test_click_coord` | 会真的移动鼠标 / 抓屏；CI 里只能跑 `--dry-run`、`--selfcheck` 之类的纯函数分支 |

> 另：`test_align.py`、`test_model.py` 是 25 个脚本里**唯二没有自动判定**的（既无 `return 1`，
> 也无 `check()`），只打印结果供人看。`test_model.py` 已归入 B 档，`test_align.py` 属纯逻辑、
> 加几行判定即可进 A 档。

**改法**：`tools/test_*.py` 不动（它们已可独立运行），新增一个薄 runner 把它们当子进程跑并聚合退出码，
CI 里一条命令搞定；后续再逐步迁到 pytest（`subprocess` + `assert rc == 0` 是最省事的过渡）。

```yaml
# .github/workflows/tests.yml（示意）
- run: pip install -r requirements.txt
- run: python tools/test_guard.py && python tools/test_position.py && python tools/test_geometry.py
```

### 3.3 已有但可以更强的地方

- `tools/verify_repo.py` / `verify_release.py` / `verify_paths.py` 已经覆盖"发布自检"，
  适合作为 release checklist 的自动化入口；`tools/build_exe.py` 的 `smoke_test()`（`:158-201`）
  会真的跑一次打包后 exe 的 `--selftest` 并检查退出码 —— 这条链路是**目前最接近 CI 的东西**，
  值得保留并纳入 workflow。
- 缺 ruff/black 与 pre-commit。以本仓库的注释密度和中文注释风格，`ruff` 只开
  `E,F,W,I,UP,B` 这类安全规则集即可，避免大规模格式噪音。

---

## 4. 架构与可维护性

### 4.1【P1】两个上帝类

AST 实测：

| 单元 | 位置 | 行数 |
|---|---|---|
| `class Worker` | `app_vision.py:192-1530` | **1338** |
| `class MainWindow` | `app_vision.py:1700-2862` | **1162** |
| `Worker.run()` | `app_vision.py:430` | **279**（嵌套深度 5） |
| `MainWindow.__init__` | `app_vision.py:1701` | **175** |
| `MainWindow._build_automove_panel` | `app_vision.py:2210` | **122** |
| `Worker._build_diag` | `app_vision.py:922` | 101 |
| `Worker._pump_auto_move` | `app_vision.py:1067` | 85 |

`run()` 一个函数承担了：模型加载/预热、引擎启动、窗口状态检查、抓帧、推理、朝向自检、
几何闸门、动画抑制、自动判执子、帧一致性确认、演化校验、局面校验、棋规校验、开局库查询、
搜索发起、引擎异常恢复、UI 信号发射 —— 17 件事。

**建议拆法**（按现有边界，风险最低的切法）：
1. `Worker.run()` 里的闸门链抽成 `pipeline`：`_gate_window()` / `_gate_geometry()` /
   `_gate_settle()` / `_gate_confirm()` / `_gate_evolution()`，每个返回
   `(ok, reason, note)`，`run()` 只负责编排与信号。**这一步同时让 1.1 的预闸门有地方放。**
2. `MainWindow.__init__` 的面板构建拆成 `_build_engine_panel()` / `_build_automove_panel()`（已存在）/
   `_build_book_panel()` / `_build_diag()`（已存在）的显式调用序列。
3. `app_vision.py` 已经天然分成三段：纯函数（`71-110`）、`BoardView`（1535）、`Worker`、`MainWindow`。
   把纯函数 + 闸门搬到 `app_gates.py`，`app_vision.py` 只留 Qt 层，文件从 2885 行降到 ~2000 行。

### 4.2【P3】30 处 `except Exception: pass`

`app_vision.py` 30 处、`app_backend.py` 20 处、`app_book.py` 3 处、`app_input.py` 2 处（AST 统计）。
抽样看，多数是**合理**的清理路径或信号发射保护，例如：

- `app_vision.py:566/590/605` —— `self.settle_state.emit(...)` / `side_detected.emit(...)` 外包一层；
- `app_vision.py:721/730` —— `shutdown()` 里 `send("stop")` 与关闭日志句柄；
- `app_backend.py:405-421` —— GDI 句柄释放。

问题不在"吞异常"，而在**吞了不留痕**。建议加一个 `swallow(what: str)` 上下文管理器，
在 `except` 里写一行 `self._log(f"忽略: {what}: {exc!r}")`，行为不变但排障时有迹可循
（仓库里 `XiangQiLens_crash.log` 只覆盖未捕获异常，覆盖不到这些分支）。

### 4.3【P2】开局库两套 SQLite 实现重复

`app_book.py`：

| 逻辑 | `XqbBook` | `ObkBook` |
|---|---|---|
| 只读连接 | `250` | `535` |
| 查询缓存 dict | `254` | `539` |
| 缓存命中 | `289-290` | `571-572` |
| 缓存淘汰（>512 全清） | `319-320` | `609-610` |
| `close()` | `324-327` | `614-616` |

约 40 行 ×2 的同构代码。抽 `class _SqliteBook`（`connect_ro()` / `_cached(board, key, sql)` / `close()`）
让 `XqbBook`、`ObkBook` 继承即可。顺带：缓存淘汰策略是"超 512 就整表清空"（`319`），
换成 `functools.lru_cache(maxsize=1024)` 或 `OrderedDict` 的 LRU 更平滑，避免周期性抖动。

### 4.4【P3】`parse_info_line` 嵌套深度 11

`app_backend.py:650-706`，一个 30 分支的 `if/elif` 链解析 UCI `info` 行，AST 测得嵌套深度 **11**。
逻辑是对的（含 `lowerbound/upperbound` 截断保护、mate→cp 归一化），但可读性差。
可改为表驱动：

```python
_SIMPLE = {"depth": "depth", "seldepth": "seldepth", "nodes": "nodes",
           "nps": "nps", "hashfull": "hashfull", "time": "time_ms"}
```

`score`/`wdl`/`multipv`/`pv` 四个特殊键单独处理，`else: i += 1` 兜底不变。

### 4.5【P3】`src/` 旁路实现与一次性脚本

- `src/` 4 个模块共 **2048 行**（`capture.py` 530 + `engine.py` 556 + `vision.py` 588 + `xiangqi.py` 374，
  早期自研"几何+颜色+模板"路线），README `:239-247` 已明确说明
  "保留作为参考"。仍有 **7 个 tools 脚本**依赖它（`analyze_board.py`、`annotate_full.py`、
  `build_templates.py`、`compare_grids.py`、`crop_points.py`、`detect_grid_pieces.py`、`test_align.py`）。
  建议整体移到 `legacy/`（连同那 7 个脚本），让 `src/` 不再与 `app_*.py` 争夺"源码目录"语义。
- 已应用的一次性脚本仍在 `tools/` 且仍可执行：`patch_logging.py`（`:86-108` 会**直接改写
  `app_vision.py`**，靠 `if new in src` 做幂等，实测是幂等的，重跑安全）、`patch_tracker.py`、
  `extract_model.py` / `extract_model2.py` / `extract_model3.py`、`probe_cchess_coords.py` / `...2.py`。
  建议移入 `tools/_archive/`。

---

## 5. 文档与注释一致性（含安全相关）

### 5.1【P1】自动走棋的默认值：四处说法，两种结论

**代码事实**：默认**真点落子**。

```
app_vision.py:2244  self.radio_real = QRadioButton("启动走棋（真点落子）")
app_vision.py:2245  self.radio_real.setChecked(True)     # 默认开启（用户要求）
app_vision.py:2450  mode = str(data.get("mode", "real"))  # 无 automove.json 时也落到真点
```

**但同一文件里的注释说的是另一回事**：

```
app_vision.py:2238  # ... 默认「关闭」，最安全。        ← 与 2245 直接矛盾
app_vision.py:2429  **刻意不持久化「启用」开关** —— 每次启动都从关闭状态开始，  ← 与 2450 矛盾
app_vision.py:2446  # ★ 启动一律回到安全态：即使上次存的是「真点」，本次也只恢复成「预览」 ← 与 2450 矛盾
app_vision.py:2448  # 默认「启动走棋（真点落子）」—— 按需求自动走棋默认开启。      ← 与 2446 矛盾
```

**README 同样自相矛盾**：

| 位置 | 说法 |
|---|---|
| `README.md:8-9` | "**默认不含自动走子**：只给建议，落子由你自己操作。另有可选的「自动走棋」（默认关闭 + 预览模式）" |
| `README.md:46` | "三态互斥单选，**默认「启动走棋（真点）」**" |
| `README.md:356` | "## 自动走棋（默认开启）" |
| `README.md:375` | "**默认就是「启动走棋（真点落子）」** —— 点「分析」就会真的替你落子" |
| `README.md:508` | "「自动走棋」**默认开启**" |

`README.md:8-9` 是**旧版残留**（那时默认预览）。这个不一致不只是文案问题：它涉及
"程序会不会替你点鼠标"这一行为预期，而 `app_vision.py:2238/2429/2446` 的注释会让下一个维护者
按"默认关闭最安全"去改代码。

**改法**：以代码为准（默认真点），改 `README.md:8-9` 与 `app_vision.py:2238/2429/2446` 三处注释；
把"默认值"收敛到一处常量（如 `DEFAULT_AUTO_MODE = "real"`）并在 UI 提示、README、配置回填处引用，
避免第四次漂移。

### 5.2【P3】其它

- `README.md` 的工具表（`:251-275`）列了约 20 个脚本，`tools/` 下实有 **60 个 `.py`** ——
  表是"精选"而非全量，建议补一行说明，否则新人会以为脚本缺失。
- `docs/` 下 8 份 `RELEASE_v*.md` 格式不一（1.6 KB ~ 17.5 KB），且没有统一的发布检查清单；
  `tools/verify_repo.py` + `verify_release.py` 已是检查清单的雏形，可生成一份
  `docs/RELEASE_CHECKLIST.md` 固化下来。

---

## 6. 健壮性小项

| 位置 | 问题 | 建议 |
|---|---|---|
| `app_vision.py:689` | `_start_search` 抛异常时直接 `_rebuild_engine()`，**无节流**；而 `_pump_engine` 的同名重建（`:859`）有 3 s 节流（`_last_rebuild`）。引擎持续崩溃时会按局面变化频率反复 spawn 进程 | 把重建统一收敛到一个带节流的 `_rebuild_engine_throttled()` |
| `app_vision.py:1049/1188/1221/1263` | 自动走棋路径上的 `except: pass` —— 逐行核对后确认都是 `self.auto_state_changed.emit(...)` / `self.auto_preview.emit(...)` 的信号保护，非逻辑吞异常 | 见 4.2，至少落日志 |
| `xq_research/engine_client.py:48-50` | `send()` 无任何异常处理，引擎已死时抛 `OSError/ValueError`；调用方靠 `try/except` 兜（`app_vision.py:682-689`） | 在 `send()` 内包一层并抛自定义 `EngineDied`，让"重建"分支语义明确（该文件当前未入库，见 2.1） |
| `xq_research/engine_client.py:52-64, 101-105` | `wait_for` / `analyse` 用 `q.get(timeout=0.5)` 轮询；且 `wait_for` 会**消费并丢弃**队列中非目标行（单线程调用下安全，但接口有陷阱） | 加注释或在 docstring 里写明"仅限单线程顺序调用" |
| `tools/build_exe.py:117` | 找不到图标时静默跳过 | 改成硬失败（见 2.2） |

---

## 7. 建议的落地顺序

**第 1 批（当天可完成，收益最大、风险最低）**
1. `app_backend.py:357-358` 抽样校验 —— 省 23.7 ms/帧（1.2）
2. `xq_vision.py` / `engine_client.py` 入库（2.1）—— 纯 `git add`，零风险
3. 修 README + 代码注释的默认值矛盾（5.1）—— 纯文档/注释
4. 加 `requirements.txt` + 一个跑 A 档测试的 CI workflow（3.1/3.2）

**第 2 批（1~2 天）**
5. 主循环预闸门（1.1）—— 建议先做 `run()` 的闸门链拆分（4.1 第 1 步）再插入，避免继续加长 279 行的 `run()`
6. `PrintWindow` GDI 对象复用（1.3）
7. 图标入库 + 构建硬失败（2.2）

**第 3 批（可排期）**
8. `app_book.py` 两套 SQLite 去重（4.3）
9. `except: pass` 可见化（4.2）
10. `src/` 与一次性脚本归档（4.5）；本地产物清理（2.3）

---

## 附：本次评审用到的可复现命令

```powershell
# 结构 / 坏味道（AST）
python C:\Users\mango\AppData\Local\Temp\xql_audit\ast_audit.py

# 抓帧校验开销对比（683x1253 合成帧）
python C:\Users\mango\AppData\Local\Temp\xql_audit\bench_grab_check.py

# 真实窗口 PrintWindow + 三种校验的实测
python C:\Users\mango\AppData\Local\Temp\xql_audit\bench_real_grab.py

# 预闸门可行性（cvtColor / gray_small / motion_ratio）
python C:\Users\mango\AppData\Local\Temp\xql_audit\bench_pregate.py

# 测试脚本可自动化程度
python C:\Users\mango\AppData\Local\Temp\xql_audit\test_scan2.py

# 资产与仓库卫生
git log --all --oneline -- xq_vision.py engine_client.py   # 空
git ls-files | Measure-Object                              # 88
```

> 说明：基准脚本写在临时目录，**未改动 `XiangQiLink` 仓库任何文件**；本报告是本次唯一新增文件。
