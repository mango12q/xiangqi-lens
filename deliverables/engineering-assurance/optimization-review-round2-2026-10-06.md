# XiangQiLens 优化评审（第二轮）

**日期**：2026-10-06
**基线**：`main` @ `70c55ef`（工作区仅多出上一轮评审报告）
**上一轮**：`deliverables/engineering-assurance/optimization-review-2026-10-06.md`（10 项）
**范围**：`D:\opencode\XiangQiLink` 全仓库（代码 / 测试 / 构建 / 仓库卫生）
**方法**：AST 静态分析 + 本机真实微基准 + **完整跑一遍 25 个测试脚本** + `git` 取证。
**证据标注**：【实测】= 本次在本机真实运行得到；【读码】= 逐行核对源码得出。

---

## 0. 结论速览

### 0.1 上一轮 10 项的落地情况

| # | 上轮结论 | 状态 | 证据 |
|---|---|---|---|
| 3 | `xq_vision`/`engine_client` 入库 | ✅ **已落地** | `70c55ef`，`vendor/xq_research/` 已跟踪 |
| 5 | 自动走棋默认值表述矛盾 | ⚠️ **部分** | `README.md:8-9` 已改；`app_vision.py:4/2213` 仍是旧说法 |
| 1 | 静止画面仍每帧全量推理（P0） | ❌ 未落地 | `app_vision.py:512` 仍无条件 `infer()` |
| 2 | 抓帧校验 `astype(float32)`（P0） | ❌ 未落地 | `app_backend.py:368` 原样 |
| 4 | 无 `requirements.txt` / CI / lint | ❌ 未落地 | 6 个定义文件 `Test-Path` 全 False |
| 6 | 两个上帝类 | ❌ 未落地 | `Worker` 1338 行 / `MainWindow` 1169 行 |
| 7 | `app_book.py` 两套 SQLite 重复 | ❌ 未落地 | 未动 |
| 8 | 1.3 GB 本地产物 | ❌ 未落地 | 见 2.6 |
| 9 | 图标引用另一个项目 | ❌ 未落地 | `tools/build_exe.py:130` 仍指向 `refs/chessboard` |
| 10 | `src/` 旁路实现归档 | ❌ 未落地 | 未动 |

### 0.2 本轮新发现（按优先级）

| # | 优先级 | 问题 | 位置 | 证据 | 影响 |
|---|---|---|---|---|---|
| N1 | **P0** | **6/25 测试脚本在干净环境硬失败，直接阻塞「加 CI」** | `tools/test_{arrow,autoclick,automove_e2e,click_coord,e2e,live_chain}.py` | 【实测】 | CI 会报 6 个假回归 |
| N2 | **P0** | `analyse()` 超时不发 `stop`，残留 `bestmove` 会被下次搜索误认 | `engine_client.py:101-129` | 【读码】+ 子代理实测 | 结果串味 |
| N3 | **P1** | `__EOF__` 哨兵只可消费一次 → 引擎死亡后不可再检测 | `engine_client.py:46,59,66` | 【读码】+ 子代理实测 | 每次等待空转满超时 |
| N4 | **P1** | `place_move()` 的 `finally` 只还原光标，**不抬起左键** | `app_input.py:276-291` | 【读码】 | 异常时鼠标永久处于按下态 |
| N5 | **P1** | `click_at()` 丢弃 `move_to()` 返回值 → 可能点到别的窗口 | `app_input.py:260-266` | 【读码】 | 绕过 `is_point_on_window` 闸门 |
| N6 | **P1** | 打包从**未跟踪的** `RESEARCH` 取代码，分发用 `VENDOR` 副本 → 两套代码 | `tools/build_exe.py:110` vs `:154` | 【实测】哈希当前一致 | 未来必然漂移 |
| N7 | **P2** | `GetCurrentThreadId()` 未声明 `restype`（有符号）与 DWORD 比较 | `app_input.py:151` | 【读码】 | 自挂接被拒 → 置前失效 |
| N8 | **P2** | `analyse()` 多主变例下取**最后**一条 PV 的分数（最差那条） | `engine_client.py:112-120` | 【读码】 | 仅 selftest 受影响 |
| N9 | **P2** | `asset_src()` 对代码资产静默回退到未跟踪目录 | `tools/build_exe.py:53-57` | 【读码】 | 违反自身注释约定 |
| N10 | **P2** | 冒烟测试「无 `selftest.log`」只告警不判失败 | `tools/build_exe.py:197-209` | 【读码】 | 写不了盘的 exe 也算通过 |
| N11 | **P3** | `MEMORY.md` 称「CRLF 是本仓库常态」——**实测不成立** | `.workbuddy-ai/memory/MEMORY.md:199` | 【实测】82/90 文件工作区为 LF | 误导后续维护 |
| N12 | **P3** | `preview_move(self, frm, to)` 的 `to` 从未使用 | `app_input.py:268` | 【读码】 | 死参数 |

---

## 1. 性能：本轮实测数字

### 1.1 抓帧校验（上轮 #2，仍未修）

`app_backend.py:368-369` 每帧对整幅截图做 `astype(np.float32)`，只为判断「这张图不是全黑」：

```python
368  a = img.astype(np.float32)
369  if float(a.std()) > 3.0 and float((a.max(axis=2) > 12).mean()) > 0.02:
```

**本次实测**（随机噪声帧，30 次平均，判据语义保持一致的 `img[::8,::8]` 抽样版）：

| 分辨率 | 现状 | 抽样版 | 加速 | 判据一致 |
|---|---|---|---|---|
| 683×1253（JJ象棋典型） | **23.94 ms** | **0.300 ms** | **72.0x** | ✅ |
| 2560×1440 | **104.10 ms** | **1.262 ms** | **80.5x** | ✅ |

> 上轮报的是 2560×1440 为 29.54 ms，本次同一机器测得 **104.10 ms** —— 说明这一项在高分辨率下比上轮估计的还贵。
> 抽样版把 24 ms 降到 0.3 ms，对 683×1253 的窗口是**单帧预算的 ~12%**。

### 1.2 主循环预闸门（上轮 #1，仍未修）

`app_vision.py:504-619` 的顺序仍是「**先全量推理，再判断画面有没有变**」：

```
504  bgr = self.source.grab()
512  res = self._vision.infer(...)          # ★ 无条件推理（pose + 90 格分类）
553  if self.anim_suppress:
554      moving, ratio = self._frame_moving(res["warped"])   # ★ 闸门用的是推理输出
617  if self._same < self.confirm or key == self._published:
618      time.sleep(0.10); continue         # ★ 静止帧走这里，但推理已经做完了
```

**本次实测**预闸门成本（`gray_small` + `motion_ratio`，683×1253）= **2.124 ms**，
对比 README 实测的完整单帧 170 ms（DirectML）⇒ 静止帧可省掉 ~98% 的计算。

**新增建议**（上轮未提）：预闸门应**限制在棋盘区域内**。整窗差分遇到秒表/聊天区动画会退化成
「每帧都推理」（不更差，但也没收益）。用上一次成功识别的 `res["warp_mat"]` 反投影出棋盘 bbox，
只对该 bbox 做差分即可避开窗口内无关动画。

**另一处本轮新发现**：`confirm = 3`（`app_vision.py:220`）意味着**首帧结果要等 3 帧一致**。
按 170 ms/帧算，点「分析」到出第一个建议约 **0.5~0.8 s**（CPU 版 ~3 s）。
预闸门落地后这个延迟会更明显 —— 建议首帧（`self._published` 为空时）**强制放行**，跳过 confirm 等待。

---

## 2. 新发现详解

### 2.1【P0】N1：6 个测试脚本在干净环境硬失败 —— 这会直接毁掉「加 CI」

**背景**：`309b2df` 已经给 `test_automove_gates.py` 做了「无可用窗口 → 打印 `[跳过]` + `return 0`」的处理
（`tools/test_automove_gates.py:88-90`），正是为了避免发布校验把环境问题误报成回归。

**本次实测**：把 25 个脚本**逐个真实执行**并收集退出码：

| 结果 | 数量 | 脚本 |
|---|---|---|
| 通过（rc=0） | **19** | `test_align` `test_automove_gates` `test_book` `test_book_codec` `test_flip` `test_geometry` `test_guard` `test_legality` `test_model` `test_newgame` `test_position` `test_screen_region_guard` `test_settle_gate` `test_turn` `test_wdl` `test_window_filter` `test_window_score` `test_winrate` `test_winstate` |
| **失败（rc=1）** | **6** | `test_arrow` `test_autoclick` `test_automove_e2e` `test_click_coord` `test_e2e` `test_live_chain` |
| 明确跳过 | 0 | —— |

**6 个失败全部是环境问题**（本机没有开 JJ象棋 / 天天象棋 窗口），不是代码回归：

```
test_arrow.py:62           print("[失败] 找不到 JJ象棋 窗口");  return 1
test_autoclick.py:61       print("[失败] 找不到 JJ象棋 窗口（可用 --hwnd 指定）");  return 1
test_automove_e2e.py:50    print("[失败] 找不到 JJ象棋 窗口（可用 --hwnd 指定）");  return 1
test_click_coord.py:96     print("[失败] 找不到 JJ象棋 窗口（可用 --hwnd 指定）");  return 1
test_e2e.py:45             print("[失败] 找不到 JJ象棋 窗口");  return 1
test_live_chain.py:44      print(f"[失败] 找不到含 {args.keyword!r} 的窗口");  return 1
```

**为什么这是 P0**：上轮评审的 #4 建议「加一个跑 A 档测试的 CI」。但**今天加上去，CI 立刻是红的**，
而且是 6 个假红。维护者第一步就会学会「忽略 CI 失败」，CI 从此失去意义。

**改法**（照抄已修好的 `test_automove_gates.py:88-90` 的模式，6 处各加 3 行）：

```python
    hwnd = find_target_window()
    if not hwnd:
        print("  [跳过] 未找到 JJ象棋 窗口（需要真实对局窗口才能验证）。")
        print("         手动验证：先打开对局界面，再重跑本脚本。")
        return 0                      # ← 关键：0 而不是 1
```

**配套**：新增 `tools/run_tests.py`（或 `pytest` 薄封装），把 25 个脚本当子进程跑并聚合退出码，
输出 `PASS 19 / SKIP 6 / FAIL 0`。这是 #4 的前置条件。

### 2.2【P0/P1】N2/N3/N8：引擎客户端的协议层缺陷

**先说清影响面**（本轮核对 `grep` 得到的事实，避免上纲上线）：

| API | 主链路（`app_vision`/`app_backend`） | 仅 selftest |
|---|---|---|
| `send` / `drain` / `wait_for` / `isready` / `quit` | ✅ 在用 | |
| `analyse` | | ✅ 仅 `app_backend.py:1791` |

所以 `analyse()` 内部的问题（N2/N8）**只影响 `--selftest`**，严重度要下调；
`wait_for`/`send`/`isready` 的问题（N3、写已死进程）**在主链路上**。

**N2 —— `analyse()` 超时不发 `stop`**（`engine_client.py:101-129`）

超时分支直接 `return`，此时引擎**仍在搜索**：后续 `info` 行无人消费地灌进无界队列，
而下一次 `analyse()` 会把这段残留的 `bestmove` 当成**新局面的**结果。

```python
101  while time.time() - t0 < timeout:
...
129  return {"bestmove": best, "ponder": None, ...}    # ← 没有 send("stop")
```

改法：用 `try/finally` 包住，未拿到 `bestmove` 就 `send("stop")` + `wait_for("bestmove", 1.0)`；
并给 `self.q` 一个 `maxsize`，超时路径先 `drain()`。

**N3 —— `__EOF__` 哨兵只可消费一次**（`engine_client.py:46,59,66`）

`_read_loop` 在 EOF 时 `put("__EOF__")` **一次**；`wait_for` 一旦读到它就 `break`
（且**不把哨兵放回队列**），`drain()` 更是直接把它吃掉。于是引擎死亡后：

```
第 1 次 wait_for("never", 1.0) → 0.00 s   （读到哨兵，立即返回）
第 2 次 wait_for("never", 1.0) → 1.01 s   （队列空了，空转满超时）
isready(timeout=1.0)           → 1.02 s，返回 []   ← 无法区分「引擎死了」和「没响应」
```

**为什么主链路目前没炸**：`app_vision.py:869` 用的是 `self._engine.proc.poll() is not None`
—— 这是**独立于哨兵**的可靠死亡检测。所以 N3 的实际影响是：
`_stop_search`（`app_vision.py:791`，`timeout=0.5`）在引擎已死时会空转 0.5 s。

改法：加 `self._dead = threading.Event()`，在 `_read_loop` 结束时 `set()`，
所有等待循环顶部检查它；哨兵改为「读后放回」或干脆由 `_dead` 取代。

**N8 —— MultiPV 下取最后一条 PV 的分数**（`engine_client.py:112-120`）

`multipv>1` 时引擎按 multipv **升序**逐条打印，代码无条件覆盖 `score_cp`：

```python
112  if " score " in line:
114      seg = line.split(" score ", 1)[1].split()
116      score_cp = int(seg[1])          # ← 最后一条 multipv 覆盖掉 multipv 1
```

子代理用假引擎验证：multipv 1 `cp 300` / 2 `cp 40` / 3 `cp -1200` → 返回 **`score_cp=-1200`**（应为 300）。

改法：只在 `multipv` 缺省或 `== 1` 时接受分数。

**N4/N5/N7（`app_input.py`）—— 见下节。**

### 2.3【P1/P2】N4/N5/N7/N12：鼠标注入层的三个真实缺陷

**N4 —— `place_move()` 异常时不抬左键**（`app_input.py:272-291`）

```python
276  try:
277      if self.mode == "drag":
280          self._down()
282          self.move_to(to[0], to[1], steps=max(4, self.move_steps))
284          self._up()
289  finally:
290      if self.restore_cursor and self._saved is not None:
291          move_to(*self._saved)          # ← 只还原光标，不 _up()
```

`_down()` 与 `_up()` 之间（`move_to` 分步移动 + 3 次 `time.sleep`）任何异常逃逸，
**左键会一直保持按下**，用户桌面上表现为「拖动粘住」直到手动点一下。
`click_at()`（`:260-266`）同样结构。

改法：维护 `self._btn_down` 标志，`finally` 里 `if self._btn_down: self._up()`。

**N5 —— `click_at()` 丢弃 `move_to()` 的返回值**（`app_input.py:260-266`）

```python
260  def click_at(self, x, y) -> None:
262      self.move_to(x, y)          # ← 返回值被丢掉
264      self._down()                # ← SetCursorPos 失败时光标还在原处，照点
```

`move_to` 会 `return bool(_u32.SetCursorPos(...))`（`:111`），但这里不看。
`SetCursorPos` 失败（例如目标坐标在虚拟桌面之外）时，点击会落在**上一个光标位置** ——
而自动走棋的安全闸门 `is_point_on_window` 校验的是**预期落点**，于是闸门被绕过。

改法：`if not self.move_to(x, y): raise RuntimeError("光标移动失败，已放弃点击")`。

**N7 —— `GetCurrentThreadId()` 未声明 `restype`**（`app_input.py:151`）

```python
150  fg_thread  = _u32.GetWindowThreadProcessId(fg, None)   # restype = wintypes.DWORD（无符号）
151  cur_thread = int(_k32.GetCurrentThreadId())            # 未声明 → ctypes 默认 c_int（有符号）
154  if fg_thread and fg_thread != cur_thread:              # 有符号 vs 无符号比较
155      attached = bool(_u32.AttachThreadInput(fg_thread, cur_thread, True))
```

TID ≥ `0x80000000` 时 `cur_thread` 为负，与无符号的 `fg_thread` 永不相等；
若二者**本是同一线程**，就会对自身 `AttachThreadInput` —— Windows 拒绝（返回 0），
`attached=False`，绕前台锁的机制失效，`SetForegroundWindow` 更容易被拒 → 本次点击被跳过。

改法：`_k32.GetCurrentThreadId.argtypes = []` / `.restype = wintypes.DWORD`，两侧统一 DWORD。

**N12 —— `preview_move(self, frm, to)` 的 `to` 从未使用**（`app_input.py:268-270`），纯死参数。

> **本轮核对为「无问题」的两处**（避免误改）：
> `AttachThreadInput` **配对正确** —— `attached` 仅在调用返回真时置位（`:155`），
> detach 在 `finally`（`:160-165`）；`_INPUT`/`_MOUSEINPUT` 结构与 x64 ABI 一致
> （实测 `sizeof` 为 40 / 32 字节），`dwExtraInfo` 用 `c_void_p` 对应 `ULONG_PTR` 正确。

### 2.4【P1/P2】N6/N9/N10：打包链路会分发「另一份代码」

**N6 —— PyInstaller 从 `RESEARCH` 取代码，分发时却用 `VENDOR` 副本**

```python
 29  RESEARCH = ROOT.parent / "xq_research"        # 资源目录（不入库）
 30  VENDOR   = ROOT / "vendor" / "xq_research"    # 代码目录（入库）
...
110      "--paths", str(RESEARCH),                 # ★ 打包时从「不入库」的目录取代码
112      "--hidden-import", "xq_vision",
113      "--hidden-import", "engine_client",
...
154      src = asset_src(rel)                      # ★ 分发时优先取 VENDOR（:53-56）
157      shutil.copy2(src, dst)
```

即**同一次发布里可能塞进两份不同版本的同一个模块**：exe 内部是 `RESEARCH` 那份，
同级 `xq_research/` 是 `VENDOR` 那份；而 `app_backend.py:83-85` 把 `RESEARCH` 插到
`sys.path[0]`，运行时**外置的 `.py` 会遮蔽 bundle 内的副本**。

**本次实测**：四处副本当前**字节一致**（`xq_vision.py` = `1623B423…`，
`engine_client.py` = `AC235403…`，`vendor` / `xq_research` / `_build_ok` / `dist` 全同），
所以现在是**潜伏**状态 —— 一旦只改了 `vendor/` 就会漂移，而且**不会有任何报错**。

改法：`--paths` 指向 `VENDOR`（或一个由 `asset_src()` 填充的暂存目录），
让「打包用的代码」与「分发的代码」是同一次 `asset_src()` 调用的产物。

**N9 —— `asset_src()` 对代码资产静默回退**（`tools/build_exe.py:51-57`）

```python
53  if rel in CODE_ASSETS:
54      p = VENDOR / rel
55      if p.is_file():
56          return p
57  return RESEARCH / rel          # ← vendor 缺失时静默用未跟踪的那份
```

文件头 `:46-47` 的注释明说「打包一律优先用入库副本，否则会出现『发出去的 exe 跑的是
`xq_research/` 里的未跟踪文件』」—— 但 `asset_src` 的兜底恰恰允许了这件事，
且 `check_env()`（`:79`）校验的是这个兜底路径，所以**构建照样通过**。

改法：`CODE_ASSETS` 的 `VENDOR` 副本缺失时**硬失败**并打印明确原因。

**N10 —— 冒烟测试的「无日志」只告警**（`tools/build_exe.py:197-209`）

```python
205      else: print("    [警告] 未生成 selftest.log")
207  if rc == 0: return True
```

判定只看 `rc == 0`。而 `_write_selftest_log`（`app_backend.py:1811-1817`）**吞掉所有写入异常**
⇒ 一个**写不了盘**的 exe 也能「通过冒烟测试」。

改法：日志缺失（或其中没有 `[通过]` 行）时返回 `False`。

**N11 附注**：`build_exe.py:130` 的图标仍指向 `ROOT.parent / "refs" / "chessboard" / ...`
（上轮 #9）。本次实测该路径在本机**存在**（`Test-Path` → True），所以本机构建没问题，
但它是**另一个仓库**，换机即静默丢图标。建议 `assets/icon.ico` 入库 + 缺失时硬失败。

### 2.5【P3】N11：`MEMORY.md` 的换行符结论与实际不符

`MEMORY.md:199` 写着「**CRLF 是本仓库常态**，不算问题」。**本次实测推翻了这个说法**：

```
$ git ls-files --eol | 按 (索引/工作区) 分组统计   总文件数 90
   82  w/lf      工作区 LF      ← 绝大多数
    3  w/crlf    工作区 CRLF    README.md / app_backend.py / app_vision.py
    2  w/-text   二进制
    1  w/crlf    启动XiangQiLens.bat（.gitattributes 明确要求 CRLF，正确）
    1  w/none    src/__init__.py（空文件）
    1  w/-text   （同上类）
```

且 `.gitattributes:2` 是 `* text=auto eol=lf`，**索引侧 90 个文件全是 `i/lf`**。
结论应改为：**仓库规范是 LF；工作区里 3 个文件残留 CRLF，属无害偏差**（`git status` 干净，
因为索引已规范化）。这条笔记会让后续维护者去做无意义的「编码修复」，建议直接更正。

### 2.6【P2】上轮 #8：本地产物已达 1.34 GB

| 目录 | 体积 | 状态 |
|---|---|---|
| `dist/` | 673.4 MB | 已 gitignore（含一份完整 `xq_research` 拷贝） |
| `_build_ok/` | 453.9 MB | 已 gitignore |
| `engines/` | 106.2 MB | 已 gitignore |
| `build/` | 46.0 MB | 已 gitignore |
| `analysis/` | 40.1 MB | 已 gitignore |
| `shots/` | 12.0 MB | 已 gitignore |
| `probe_out/` | 4.8 MB | 已 gitignore |
| **合计** | **≈ 1,336 MB** | 仓库本身干净（90 个跟踪文件） |

另有两处**已 gitignore 但仍在工作区**的残留：`app_vision.py.bak`（34 KB）、
`selftest.log` / `XiangQiLens.log` / `XiangQiLens_crash.log` / `_launch_log.txt` / `restart_btn.png`。
`*.log` / `*.bak` / `*.png` 都在 `.gitignore` 里，所以不影响提交，但会干扰「工作区有什么」的判断。

---

## 3. 建议的落地顺序

**第 1 批（当天，解锁后续一切）**
1. **N1**：6 个测试脚本改成「明确跳过 + `return 0`」；新增 `tools/run_tests.py` 聚合退出码
2. **#4**：`requirements.txt` + `requirements-dev.txt` + `.github/workflows/tests.yml`（依赖 1 才能绿）
3. **#2**：`app_backend.py:368-369` 改抽样校验 —— **实测省 23.7 ms/帧（72x）**，风险极低
4. **N11**：更正 `MEMORY.md:199` 的换行符结论（纯文档）

**第 2 批（1~2 天）**
5. **#1**：主循环预闸门（先按上轮 4.1 拆 `run()` 的闸门链，避免 279 行的函数继续变长）
6. **N4/N5**：`place_move` 的 `finally` 抬左键；`click_at` 检查 `move_to` 返回值
7. **N6/N9/N10**：打包链路统一走 `asset_src()`；代码资产缺失硬失败；冒烟测试判日志

**第 3 批（可排期）**
8. **N2/N3/N8**：`engine_client.py` 加 `stop`/`_dead` 事件/MultiPV 分数取 multipv 1
9. **N7**：`GetCurrentThreadId` 声明 `restype = DWORD`
10. **#7** `app_book.py` 去重 · **#6** 上帝类拆分 · **#9** 图标入库 · **#10** `src/` 归档 · **#8** 清理产物

---

## 附：本次评审的可复现命令

```powershell
cd D:\opencode\XiangQiLink

# 25 个测试脚本的真实退出码（本次结论 N1 的来源）
foreach ($f in (Get-ChildItem tools\test_*.py | Sort-Object Name)) {
    & python $f.FullName *> $null
    "{0,-32} rc={1}" -f $f.Name, $LASTEXITCODE
}

# 抓帧校验开销对比（本次 1.1 的数字）
#   astype(float32) vs img[::8,::8] 抽样，683x1253 与 2560x1440，各 30 次取均值

# 上帝类规模
python -c "import ast;t=ast.parse(open('app_vision.py',encoding='utf-8').read());[print(n.name,n.lineno,n.end_lineno-n.lineno+1) for n in ast.walk(t) if isinstance(n,ast.ClassDef)]"

# 换行符真值（本次 N11 的来源）
git ls-files --eol
git check-attr text eol -- app_vision.py

# 四份代码副本是否一致（本次 N6 的来源）
foreach ($n in 'xq_vision.py','engine_client.py') {
  foreach ($p in "vendor\xq_research\$n","..\xq_research\$n","_build_ok\xq_research\$n","dist\XiangQiLens\xq_research\$n") {
    if (Test-Path $p) { "{0,-50} {1}" -f $p, (Get-FileHash $p -Algorithm SHA256).Hash.Substring(0,16) }
  }
}

# 项目定义文件（本次 #4 的来源）
foreach ($f in 'requirements.txt','pyproject.toml','pytest.ini','conftest.py','.github') { "{0,-20} {1}" -f $f, (Test-Path $f) }
```

> 说明：本报告是本次评审**唯一新增的文件**，未改动仓库任何现有文件（含代码与文档）。
> 所有基准脚本写在 `%TEMP%` 下。
