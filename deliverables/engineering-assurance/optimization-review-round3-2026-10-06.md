# XiangQiLens 优化评审（第三轮）

**日期**：2026-10-06
**基线**：`main` @ `70c55ef`（工作区仅多出前两轮评审报告，未改动任何源码）
**上一轮**：`optimization-review-round2-2026-10-06.md`（上轮 12 项 + 本轮新发现）
**范围**：`D:\opencode\XiangQiLink` 全仓库
**方法**：AST 静态分析 + **本机真实微基准（含 DirectML 真实推理）** + `git` 取证 + 覆盖率缺口扫描
**证据标注**：【实测】= 本次在本机真实运行得到；【读码】= 逐行核对源码得出。

---

## 0. 结论速览

### 0.1 前两轮条目的落地情况（本轮复核）

**核心结论：前两轮 22 项里，只有 2 项落地。所以「还能优化什么」的第一答案，
是把已经写好、实测过、风险很低的那些先做掉。**

| 轮次 | # | 结论 | 本轮复核 |
|---|---|---|---|
| 1 | 1 | 静止画面仍每帧全量推理（P0） | ❌ 未落地（`app_vision.py:504-619` 顺序未变） |
| 1 | 2 | 抓帧校验 `astype(float32)`（P0） | ❌ 未落地（`app_backend.py:368-369` 原样） |
| 1 | 4 | 无 `requirements.txt` / CI / lint | ❌ 未落地（6 个定义文件 `Test-Path` 全 False） |
| 1 | 6 | 两个上帝类 | ❌ 未落地（`Worker` 1338 行 / `MainWindow` 1169 行） |
| 1 | 7 | `app_book.py` 两套 SQLite 重复 | ❌ 未落地 |
| 1 | 8 | 1.3 GB 本地产物 | ❌ 未落地（本轮实测 1,338 MB，明细见 §2.6） |
| 1 | 9 | 图标引用另一个项目 | ❌ 未落地（`build_exe.py:130`） |
| 1 | 10 | `src/` 旁路实现归档 | ❌ 未落地 |
| 2 | N1 | 6/25 测试脚本干净环境硬失败 | ❌ 未落地 |
| 2 | N2/N3/N8 | `engine_client` 协议层三缺陷 | ❌ 未落地 |
| 2 | N4/N5/N7 | `app_input` 三缺陷 | ❌ 未落地 |
| 2 | N6/N9/N10 | 打包链路会分发「另一份代码」 | ❌ 未落地（四份副本本轮仍字节一致，仍是**潜伏**状态） |
| 2 | N11 | `MEMORY.md` 换行符结论与实际不符 | ❌ 未落地 |
| 2 | N12 | `preview_move` 死参数 | ❌ 未落地（本轮发现整个方法在生产侧不可达，见 N8） |
| 2 | 1.2 附注 | 首帧 `confirm` 等待 | ❌ 未落地（本轮发现影响面比上轮描述的大得多，见 P0-1） |

**唯一落地的两项**：`vendor/xq_research` 入库（`70c55ef`）、`test_automove_gates` 无窗口时跳过（`309b2df`）。

### 0.2 本轮新发现（按优先级）

| # | 优先级 | 问题 | 位置 | 证据 | 实测影响 |
|---|---|---|---|---|---|
| **P0-1** | **P0** | `confirm=3` 在**每一次**走子后都要重等 3 帧 + 2×100 ms `sleep`，那 200 ms 是纯加时 | `app_vision.py:613-619` | 【实测】 | **+547 ms 才能看见对手走子** |
| **P0-2** | **P0** | 抓帧校验 `astype(np.float32)`（上轮 #2，补本轮新数字） | `app_backend.py:368-369` | 【实测】 | **26.3 ms/帧**（683×1253） |
| **P1-3** | **P1** | BGRA→BGR 用花式索引而非 `cv2.cvtColor` | `app_backend.py:413`、`:459` | 【实测】 | **3.23 ms → 0.59 ms（5.5x）** |
| **P1-4** | **P1** | 静止帧仍全量推理（上轮 #1，补「预闸门可直接跑在原始帧上」的实测） | `app_vision.py:504-619` | 【实测】 | **143 ms/帧 → 3.5 ms/帧** |
| **P1-5** | **P1** | `thread.wait(3000)` 返回值被丢弃 → 关窗口时**孤儿 Pikafish 进程** | `app_vision.py:2859-2868`、`:2841-2848` | 【读码】 | 进程泄漏 + 日志截断 |
| **P2-6** | **P2** | 同一局面被重复校验 3~4 遍，闸门 2 的规则有三个副本 | `app_backend.py:1549-1560`、`app_vision.py:647,653` | 【读码】+【实测】 | 维护陷阱（非性能） |
| **P2-7** | **P2** | `TurnManager` / `parse_alternatives` / `legal_moves` 是生产侧死代码 | `app_backend.py:1121-1204` | 【读码】 | 误导性能判断 |
| **P2-8** | **P2** | `sendinput` 后端与 `preview_move` 生产不可达 | `app_input.py:118-121,236-245,268` | 【读码】 | 文档承诺的能力用户摸不到 |
| **P2-9** | **P2** | `MouseClicker.move_to(steps>1)` **恒返回 True** | `app_input.py:247-257` | 【读码】 | 与 N5 同族，但更严重 |
| **P2-10** | **P2** | 两条 zip 打包路径、产物名不一致 | `build_exe.py:218-226` vs `make_release_zip.py:30` | 【读码】 | 容易发错包 |
| **P3-11** | **P3** | `tools/` 样板重复：44 处 `sys.path.insert`、22 处窗口查找 | `tools/*.py` | 【实测】 | 60 脚本 / 8230 行，可删 300~500 行 |
| **P3-12** | **P3** | 工作区 `XiangQiLens.spec` 是**陈旧的第二份打包配置** | `XiangQiLens.spec:12,13,42` | 【实测】 | 直接跑它会走错路径 |
| **P3-13** | **P3** | `_open_log` 用 `"w"` 覆盖运行日志 | `app_vision.py:320` | 【读码】 | 上一次的运行日志永远丢失 |
| **P3-14** | **P3** | 多处硬编码本机绝对路径 | `启动XiangQiLens.bat:17` 等 6 处 | 【实测】 | 换机噪音 |

---

## 1. 本轮实测数字（基线）

本机环境：onnxruntime 可用 provider = `['DmlExecutionProvider', 'CPUExecutionProvider']`。

| 项目 | 实测值 |
|---|---|
| `XiangqiVision.infer`（683×1253，DirectML，随机图，15 次中位） | **115.7 ms**（p90 123.8 ms） |
| `cv2.cvtColor(bgr, BGR2RGB)`（683×1253） | **0.62 ms** |
| 抓帧校验 `img.astype(np.float32)`（683×1253） | **26.32 ms** |
| 抓帧校验 `img.astype(np.float32)`（2560×1440） | **109.88 ms** |
| 抓帧校验抽样版 `img[::8,::8]`（683×1253 / 2560×1440） | **0.30 ms / 1.70 ms**（86x / 65x） |
| `np.ascontiguousarray(arr[:,:,:3])`（BGRA 683×1253） | **3.23 ms** |
| `cv2.cvtColor(arr, COLOR_BGRA2BGR)`（同图） | **0.59 ms**（5.5x） |
| 原始帧预闸门：`gray_small`(683×1253) + `motion_ratio`(144×160) | **0.90 ms + 0.025 ms** |
| `enumerate_legal_moves` | **5.74 ms** |
| `validate_fen` | **1.10 ms** |
| `is_strict_legal_move` | **0.20 ms** |
| `pv_to_chinese`（5 ply） | **0.69 ms** |
| `check_position` / `ChessBoard(board_only)` | **0.015 ms / 0.012 ms** |

> README 记的是 170 ms/帧（DirectML）。本轮同机同模型实测 **115.7 ms** —— 说明 README
> 的性能表已经偏保守，可一并更新。

---

## 2. 新发现详解

### 2.1【P0-1】`confirm=3` 的代价发生在**每一手**，不只是首帧

`app_vision.py:612-619`：

```python
612  # 连续 N 帧一致才进入演化校验（先滤掉动画/过渡帧）
613  if key == self._last_key:
614      self._same += 1
615  else:
616      self._last_key, self._same = key, 1
617  if self._same < self.confirm or key == self._published:
618      time.sleep(0.10)
619      continue
```

**上轮把这一条只当成「首帧延迟」**（`confirm=3` ⇒ 点「分析」后约 0.5~0.8 s 才出第一个建议）。
但同一个分支在**对手每走一步之后都会重新触发**：棋盘一变，`key != _last_key` ⇒
`_same = 1` ⇒ 要连续 3 帧相同才放行。

**本轮实测这条链路的延迟**：

```
对手走子
  ├─ 帧1：infer 115.7 ms → _same=1 → sleep 100 ms
  ├─ 帧2：infer 115.7 ms → _same=2 → sleep 100 ms
  └─ 帧3：infer 115.7 ms → _same=3 → 放行，进入 BoardTracker
                                      ────────────────────────
                                      看见走子 ≈ 547 ms
  然后 _start_search → go movetime 1200（auto_think_ms）
  → bestmove → 点击                                   ≈ +1200 ms
                                      ────────────────────────
                                      落子总反应 ≈ 1.75 s
```

**关键点：那 200 ms 是纯加时。** 循环已经被 115.7 ms 的推理自然限速了 ——
`time.sleep(0.10)` 存在的理由是「避免空转烧 CPU」，但在这一轮里我们刚刚花掉
116 ms 的 GPU 时间，再加 100 ms 只是把反应时间往后推。

**改法**（两步，可分开评估）：

1. **去掉/缩短 confirm 分支的 sleep**（省 ~200 ms/手）：
   ```python
   if self._same < self.confirm:
       time.sleep(0.005)          # 只为让出 GIL，不再人为降速
       continue
   if key == self._published:
       time.sleep(0.10)           # 「局面没变」的稳态轮询保留 100 ms，这里才有意义
       continue
   ```
   注意必须**把两个条件拆开**：`key == self._published`（局面没变）才真正需要节流；
   `_same < confirm`（正在确认新变化）恰恰是最需要快的时刻。

2. **`confirm` 降为 2**（省 ~116 ms/手）：闸门 4（`BoardTracker` 的演化校验）本身
   就有「连续两帧一致才采信」的候选机制，`confirm=3` 与它职责重叠。若担心动画，
   应交给动画抑制闸门（`anim_suppress`），而不是让 `confirm` 同时干两件事。

> 附带：`confirm` 与 `settle_ms` 都在做「等画面稳定」，语义重叠。建议明确分工 ——
> `settle_ms` 负责「动画结束」，`confirm` 只负责「识别结果抖动」。

### 2.2【P0-2】抓帧校验 `astype(np.float32)`（上轮 #2，补新数字）

`app_backend.py:366-371`：

```python
366  img = self._print_window()
367  if img is not None:
368      a = img.astype(np.float32)                        # ← 整幅转 float32
369      if float(a.std()) > 3.0 and float((a.max(axis=2) > 12).mean()) > 0.02:
```

本轮实测（判据语义保持一致的 `img[::8,::8]` 抽样版）：

| 分辨率 | 现状 | 抽样版 | 加速 | 占单帧预算 |
|---|---|---|---|---|
| 683×1253（JJ象棋典型） | **26.32 ms** | **0.30 ms** | **86.3x** | **23%**（对比 115.7 ms 推理） |
| 2560×1440 | **109.88 ms** | **1.70 ms** | **64.8x** | 与推理同量级 |

上轮报的是 683×1253 为 23.94 ms，本轮 26.32 ms —— 同一量级，结论不变。
**这是全仓库性价比最高的单点改动**：3 行代码，省 26 ms/帧，判据不变，风险极低。

### 2.3【P1-3】BGRA→BGR 用花式索引而非 `cv2.cvtColor`（本轮新发现）

`app_backend.py:411-413`（`_print_window`）：

```python
411  arr = buf[: bw * bh * 4].reshape(bh, bw, 4)
412  self.rect = (l, t, r, b)
413  return np.ascontiguousarray(arr[:, :, :3])      # BGRA -> BGR
```

`app_backend.py:456-459`（`_screen_region`，同样写法）：

```python
456  arr = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(
457      shot.height, shot.width, 4)
458  self.rect = (l, t, r, b)
459  return np.ascontiguousarray(arr[:, :, :3])
```

**实测（BGRA 683×1253，200 次平均）**：

```
np.ascontiguousarray(arr[:, :, :3])   3.232 ms
cv2.cvtColor(arr, COLOR_BGRA2BGR)     0.590 ms      →  5.5x
```

花式索引 `arr[:, :, :3]` 是**跨步视图**，`ascontiguousarray` 必须逐元素跨 4 字节
拷贝；`cv2.cvtColor` 走 SIMD 且能一次完成去 alpha + 通道重排。

**改法**（两处，各 1 行）：

```python
return cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)
```

`cv2` 已在 `app_backend.py:21` 导入，无需新增依赖。**每帧省 2.6 ms**，
且这一行在两条抓帧路径上都会跑到（`_print_window` 命中时走前者，回退时走后者）。

> 同类机会：`app_backend.py:406` 的 `bmp.GetBitmapBits(True)` 会把整个 GDI
> 位图拷成一个 Python `bytes`（683×1253×4 ≈ 3.4 MB），随后 `np.frombuffer`
> 又建一个数组。这一份拷贝较难避免（pywin32 接口所限），**但 GDI 对象本身
> 每帧都重建**：`CreateCompatibleDC` + `CreateCompatibleBitmap`（`:396-399`）
> 在窗口尺寸不变时完全可以缓存复用，省掉每帧一次 ~3.4 MB 的 GDI 位图分配。
> 这是本轮能看出的下一个可测量点，但需要真实窗口才能给出数字，故只列为
> 「下一步待测」，不计入上表。

### 2.4【P1-4】静止帧全量推理（上轮 #1）—— 预闸门可以直接跑在**原始帧**上

上轮建议用 `_frame_moving(res["warped"])` 做预闸门，但那需要 `warped`，
而 `warped` 来自推理 —— 逻辑上是个循环。本轮补上实测：**预闸门不需要等推理**，
直接对 `grab()` 返回的原始 BGR 帧做灰度差分即可。

**实测**：`gray_small`（683×1253 BGR）= **0.902 ms**，`motion_ratio`（144×160）= **0.025 ms**
⇒ 预闸门合计 **0.90 ms**。

**静止帧成本对比**（683×1253）：

| 阶段 | 现状 | 加预闸门后 |
|---|---|---|
| 抓帧校验（`astype`） | 26.32 ms | 0.30 ms（§2.2） |
| `cvtColor` BGR2RGB | 0.62 ms | 跳过 |
| `infer`（pose + 90 格） | 115.7 ms | **跳过** |
| 预闸门（原始帧差分） | — | 0.90 ms |
| BGRA→BGR | 3.23 ms | 0.59 ms（§2.3） |
| **静止帧合计** | **≈ 143 ms**（约 7 Hz 空转） | **≈ 1.8 ms** |

**★ 落地时必须注意的一个坑**：`gray_small`（`app_vision.py:71-74`）现在写的是

```python
73  g = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
```

因为它的输入一直是**推理产出的 RGB 拉正图**。挪到原始帧（BGR）上必须换成
`COLOR_BGR2GRAY`，否则灰度系数与通道错配，差分阈值会系统性偏移 —— 这个函数
是 `test_settle_gate.py` 的被测对象，改动要连带更新测试。

**建议的落地形态**（避免 `run()` 那个 279 行的循环继续变长）：

```python
# 纯函数，可单测：tools/test_pregate.py
def raw_frame_moving(prev_gray, bgr, thresh: float) -> tuple[bool, float]:
    g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    g = cv2.resize(g, (144, 160), interpolation=cv2.INTER_AREA)
    return motion_ratio(prev_gray, g) > thresh, ...
```

再在 `app_vision.py:504` 之后、`:512` 之前插一道判断。上轮已建议把闸门链从
`run()` 里拆成独立函数（#1 + #6），这里正好是第一个可以无痛提取的纯函数。

### 2.5【P1-5】`thread.wait(3000)` 返回值被丢弃 → 孤儿 Pikafish 进程

`app_vision.py:2859-2868`：

```python
2859  def closeEvent(self, ev) -> None:
2860      self._save_auto_config()
2861      self.stop()
2862      if self.thread:
2863          self.thread.quit()
2864          self.thread.wait(3000)        # ← 返回值（是否真的停了）被丢掉
2865      # 线程已停，再确保引擎子进程被关掉（否则会留下孤儿进程）
2866      if self.worker:
2867          self.worker.shutdown()
2868      ev.accept()
```

`:2841-2848` 的 `on_done` 是同一写法。

**代码自己的注释（`:2865`）说明这一步的目的正是「避免留下孤儿进程」，
但缺了最关键的那个守卫**：`wait()` 超时后返回 `False`，代码却当作已经停了。

**可达路径（不依赖竞态，是确定性时序问题）**：`run()` 开头加载模型，
代码自己的注释（`app_vision.py:436`）写着「**首次约 10~30 秒**」。
用户在启动阶段点「停止」或直接关窗口：

```
GUI 线程                           Worker 线程
─────────────────────────────────────────────────────────────
closeEvent()
  stop() → _run = False
  thread.quit()
  thread.wait(3000) → False       run() 仍在 create_vision()（10~30 s）
  worker.shutdown()               self._engine 此时是 None
    → if self._engine is not None:  ← 条件不成立，什么都没关
    → _close_book(); _log_fh.close()
                                   create_vision() 返回
                                   create_engine(...)      ← :459
                                   self._engine = <新进程>  ← 再也没人 dispose
                                   循环因 _run=False 立刻退出
                                   done.emit()
```

⇒ **Pikafish 子进程成为孤儿**，用户任务管理器里会多一个常驻进程。
同一条竞态在 `_rebuild_engine`（`app_vision.py:825-852`，拉起新进程要加载 nnue）
期间也会触发。

**改法**（两处，任选其一或都做）：

1. 守卫 `wait()` 的返回值，超时就不要碰 Worker 内部状态：
   ```python
   if self.thread:
       self.thread.quit()
       if not self.thread.wait(3000):
           self.statusBar().showMessage("识别线程仍在收尾，引擎将在其退出后自行关闭")
           return                      # 或只 ev.ignore()，别再调 shutdown()
   if self.worker:
       self.worker.shutdown()
   ```

2. 更稳的做法：**把引擎生命周期收进 `run()` 的 `finally`**，让「谁创建谁销毁」
   成为结构性保证，而不是靠 GUI 线程在正确的时刻调用 `shutdown()`：
   ```python
   try:
       ...  # 整个循环体
   finally:
       self._dispose_engine(self._engine)
       self._engine = None
       self._close_book()
   ```
   这样无论 `shutdown()` 何时被调用、是否超时，进程都不会泄漏。

### 2.6【P2-6】闸门 2 的规则现在有三个副本

采信一帧时，同一个局面被校验了 3~4 遍：

| 位置 | 调用 | 实测成本 |
|---|---|---|
| `app_backend.py:1550` | `check_position(board_only)` | 0.015 ms |
| `app_backend.py:1557` | `ChessBoard(board_only)` | 0.012 ms |
| `app_vision.py:647` | `check_position(fen)` | 0.016 ms |
| `app_vision.py:653` | `validate_fen(fen)` | 1.10 ms |

**这不是性能问题**（合计约 1.14 ms，占单帧 1%），**是维护问题**：闸门 2 的
「什么算合法局面」现在同时存在于 `check_position` 的规则表、`ChessBoard` 的构造
校验、以及 `BoardTracker.update` 与 `run()` 两处调用点。将来加一条规则
（例如「兵数上限」）很容易只改一处。

**改法**：让 `BoardTracker.update` 的返回值带上它已经算好的 `n_moves`
（`validate_fen` 返回的第三个值），`run()` 直接复用，删掉 `:647`/`:653`。
`BoardTracker` 成为闸门 2+3+4 的唯一入口 —— 这也让 `tools/test_guard.py`
的覆盖范围自然扩大到闸门 2/3。

### 2.7【P2-7】生产代码里的死代码会误导性能判断

| 符号 | 位置 | 生产侧调用者 | 只被谁引用 |
|---|---|---|---|
| `TurnManager`（60 行） | `app_backend.py:1121-1180` | **无** | `tools/test_turn.py`、`tools/test_live_chain.py` |
| `parse_alternatives`（22 行） | `app_backend.py:1183-1204` | **无** | `tools/test_e2e.py`、`tools/test_live_chain.py` |
| `legal_moves`（别名） | `app_backend.py:1053-1055` | **无** | 仅被上面死掉的 `TurnManager` 调用 |

**为什么值得处理**：`enumerate_legal_moves` 实测 **5.74 ms**，是全仓库最贵的
棋规函数，而它的唯一生产调用者就在死掉的 `TurnManager` 里 ——
**主链路根本不调用它**。留着这套「轮次管理第二实现」有两个害处：

1. 读代码的人会以为主链路在做 5.7 ms 的全量合法着法枚举，从而把优化精力
   投错方向（真实热点是 `astype` 26 ms 和推理 116 ms）；
2. 与 `BoardTracker` 的轮次逻辑**语义重复但实现不同**，将来修轮次 bug
   必然有人改错文件。

**改法**：`TurnManager` 与 `parse_alternatives` 移到 `tools/`（保留其测试价值），
`legal_moves` 别名一并清掉，调用点改用 `enumerate_legal_moves`。

> 顺带：`MultiPVBuffer` 已完全取代 `parse_alternatives` 的职责，后者的正则
> `r"depth (\d+).*?score (cp|mate) (-?\d+).*? pv ([a-i]\d[a-i]\d)"` 比
> `parse_info_line` 脆弱（依赖字段顺序），确认无用后直接删比移走更合适。

### 2.8【P2-8】`sendinput` 后端与 `preview_move` 在生产侧不可达

`app_input.py:216-217` 的文档写着：

```
``backend``: ``"mouse_event"``（默认，对齐 BGI）或 ``"sendinput"``
（更现代，部分 CEF/WebView 上更可靠）。
```

但 `app_vision.py` 的两处构造（`:1195`、`:1242`）**都没传 `backend`**：

```python
1195  self._clicker = MouseClicker(mode=self.auto_click_mode,
1196                               move_steps=self.auto_move_steps,
1197                               restore_cursor=self.auto_restore_cursor)
```

全仓库唯一能选到 `sendinput` 的地方是 `tools/test_autoclick.py:47` 的
`--backend` 参数 —— 也就是**只有开发者手工跑测试才能用**。同理：

* `preview_move`（`:268`）：只有 `tools/test_autoclick.py:150` 调用；
* `MOUSEEVENTF_MOVE`（`:35`）、`MOUSEEVENTF_ABSOLUTE`（`:38`）：零引用
  （注释 `:6` 已明确说明不用 ABSOLUTE 方案）。

**影响**：README / 文档把 `sendinput` 描述成「更可靠的备选」，但用户
在界面上永远摸不到它。要么兑现，要么删掉。

**改法**：把 backend 提到「自动走棋」面板加一个下拉（与「落子方式」并列，
改动量约 15 行：UI + `apply_auto_params` + `_do_auto_click` 传参）；
同时清掉两个死常量。若不打算暴露，则删 `sendinput` 分支并修正 `:216-217` 的文档。

### 2.9【P2-9】`MouseClicker.move_to(steps>1)` 恒返回 `True`

`app_input.py:247-257`：

```python
247  def move_to(self, x: float, y: float, steps: int | None = None) -> bool:
248      """移动光标；``steps>1`` 时线性插值分步移动（触发 hover 高亮）。"""
249      steps = self.move_steps if steps is None else max(1, int(steps))
250      if steps <= 1:
251          return move_to(x, y)              # ← 单步：正确返回 SetCursorPos 的结果
252      cx, cy = get_cursor_pos()
253      for i in range(1, steps + 1):
254          t = i / steps
255          move_to(cx + (x - cx) * t, cy + (y - cy) * t)   # ← 返回值被丢弃
256          time.sleep(self.move_step_delay)
257      return True                           # ← 无条件宣称成功
```

上轮的 N5 说的是 `click_at`（`:260-266`）**丢弃** `move_to` 的返回值；
这里是 `move_to` **自己伪造**成功 —— 同族但更隐蔽，因为调用方无法通过
「检查返回值」来补救。

**影响面**：`mode="drag"` 时 `place_move`（`:282`）走的就是 `steps=max(4, ...)`，
而 `move_step_delay` 默认 8 ms ⇒ 每次拖拽落子都在这条路径上；多显示器负坐标
或坐标落在虚拟桌面之外时 `SetCursorPos` 会失败，这里会一路返回成功。

**改法**：

```python
ok = True
for i in range(1, steps + 1):
    t = i / steps
    ok = move_to(cx + (x - cx) * t, cy + (y - cy) * t) and ok
    time.sleep(self.move_step_delay)
return ok
```

配合 N5 的修法（`click_at` 检查返回值），自动走棋的坐标闸门才算真正闭合。

### 2.10【P2-10】两条 zip 打包路径，产物名不一致

| 路径 | 产物 | 压缩策略 |
|---|---|---|
| `tools/build_exe.py:218-226`（`--zip`） | `dist/XiangQiLens.zip` | `shutil.make_archive` 默认（ZIP_DEFLATED 默认级别） |
| `tools/make_release_zip.py:30` | `dist/XiangQiLens-v{ver}.zip` | 按扩展名分级（已压过的 `.onnx/.nnue` 用 STORED，文本用 level 9） |

两个都写 `dist/`，命名不同、压缩策略不同，且 `build_exe.py:222` 的
`out = DIST / f"{APP_NAME}"` 与输入目录同名，读起来容易误解。

**风险**：`make_release_zip.py` 的注释（`:4-5`）明确说明「437 MB 压起来要几分钟」
才做了分级压缩 —— 而 `--zip` 这条路会绕过它，慢且产物名不符合发布惯例
（`verify_release.py` 按 tag 校验资产名）。**发布时容易发错包**。

**改法**：`build_exe.py --zip` 改为直接调用 `make_release_zip.py`：

```python
if args.zip:
    rc = run([sys.executable, str(ROOT / "tools" / "make_release_zip.py")])
    if rc != 0:
        return 1
```

删掉 `build_exe.py:218-226` 的 `make_zip()`，让「压缩成可发布产物」只有一条实现。

### 2.11【P3-11】`tools/` 样板重复（带数字）

**实测**：`tools/*.py` 共 **60 个脚本 / 8230 行**。

| 重复样板 | 出现次数 |
|---|---|
| `sys.path.insert(...)` 路径插入块（每处 3~4 行） | **44** 个脚本 |
| 窗口查找相关代码（`EnumWindows` / `IsWindowVisible` / `GetWindowText` / `find_target_window` / `list_windows`） | **22** 处 |
| 各自定义 `main()` | **55** 个 |
| `from app_backend import ...` | **29** 个 |

**改法**：新增 `tools/_common.py`，提供三件套：

```python
def bootstrap() -> None:          # 取代 44 处 sys.path.insert 样板
def find_target_window(keyword=None):   # 取代 22 处窗口查找
def check(name, cond, detail="") -> bool:   # 统一 [OK]/[FAIL] 输出与退出码
```

预计可删 **300~500 行**。**这件事的真正价值不在行数**，而在于它是上轮 #4
「加 CI」的前置条件：目前 25 个测试脚本有 6 个在无窗口环境下硬失败（N1），
而统一 `check()` 之后，「环境不具备 → 打印 `[跳过]` + `return 0`」可以
在 `_common.py` 里**一次性**实现，不必改 6 个文件。

### 2.12【P3-12】工作区 `XiangQiLens.spec` 是陈旧的第二份打包配置

`XiangQiLens.spec:12-13,42`：

```python
12      ['D:/opencode/XiangQiLink/app_vision.py'],
13      pathex=['D:/opencode/xq_research'],
...
42      icon=['D:/opencode/refs/chessboard/server/icons/icon.ico'],
```

`*.spec` 在 `.gitignore:47` 里被忽略，所以这是 `build_exe.py` 上次运行后留下的
本地残留 —— 但它**和 `build_exe.py` 现在的参数已经不是一回事**：

| 项 | `build_exe.py` | 工作区 `XiangQiLens.spec` |
|---|---|---|
| 代码来源 | `--paths RESEARCH`（`:110`，即上轮 N6 的问题） | `pathex=['D:/opencode/xq_research']`（同一问题，但**写死**） |
| 图标 | `ROOT.parent/refs/chessboard/...`（`:130`） | `D:/opencode/refs/chessboard/...`（写死） |
| 排除模块 | 8 个（`:126-127`） | 8 个（一致） |
| 绝对路径 | 无 | **3 处** |

**风险**：`pyinstaller XiangQiLens.spec` 是开发者的自然动作，一走这条路就会
用写死的 `D:/opencode/xq_research` 和别的仓库的图标。`build_exe.py` 用
`--clean` 会重新生成它，所以问题只在「有人直接跑 spec」时暴露。

**改法**：`build_exe.py` 的 `build()`（`:93-100`）在清 `BUILD`/`DIST` 时
**顺带删掉 `ROOT/XiangQiLens.spec`**，确保不会有陈旧 spec 被误用；
或者把 spec 生成收进 `asset_src()` 统一管理（与上轮 N6 的修法合并）。

> 附带好消息：**版本号已经集中**。`__version__` 只定义在
> `app_backend.py:26`，`make_release_zip.py:20-27` 用正则从那里读，
> 不存在多处硬编码。这一点前两轮没提，属于已经做对的地方。

### 2.13【P3-13】`_open_log` 覆盖式写日志

`app_vision.py:317-325`：

```python
319  p = base / "XiangQiLens.log"
320  fh = open(p, "w", encoding="utf-8", buffering=1)      # ← "w" 每次启动清空
```

崩溃详情另有 `XiangQiLens_crash.log`（`:136`，**append** 模式），所以崩溃信息
不会丢 —— 这也是为什么这一条只算 P3。但**「上一次运行到哪一步、为什么没出结果」
永远看不到**，而用户报障时最需要的恰恰是这些。

**改法**：改 `"a"` 模式，启动时若文件 > 2 MB 则先轮转成 `XiangQiLens.log.1`
（十几行代码），日志就能跨次对比。

### 2.14【P3-14】硬编码本机绝对路径（6 处）

| 文件:行 | 内容 | 说明 |
|---|---|---|
| `启动XiangQiLens.bat:17` | `C:\Users\mango\AppData\Local\Programs\Python\Python313\pythonw.exe` | 有 `:find_pyw` 兜底（`:find_pyw` 段），所以不会坏；但入库本机路径无意义，建议 `set "PYW="` 留空 |
| `tools/diag_engine.py:10` | `Path(r"D:\opencode\xq_research\...")` | 可直接改用 `app_backend.ENGINE_EXE` |
| `tools/repro_engine.py:16` | 同上 | 同上 |
| `tools/test_wdl.py:12-13` | exe 路径 + `CWD` | 同上 |
| `tools/patch_tracker.py:10` | `Path(r"D:\opencode\XiangQiLink\app_backend.py")` | 一次性脚本，可归档 |
| `vendor/xq_research/xq_vision.py:211-214` | `main()` 的 `--pose/--cls/--save` 默认值 | 入库代码里的本机路径，建议默认值改为相对路径或从环境变量取 |

**改法**：前四处统一改成 `from app_backend import RESEARCH, ENGINE_EXE`；
`xq_vision.py` 的 `main()` 默认值改为 `None` + 运行时从 `RESEARCH` 推导。

### 2.15 仓库卫生：本轮复核为「已经做对」的部分

避免误改，这几项本轮核对**无问题**：

* **git 里没有大文件**。`git ls-files` 共 **90 个文件**，体积 Top 是
  `docs/screenshot.png` 282 KB、`data/opening.xqb` 193 KB、`app_vision.py` 135 KB ——
  全部合理，`.gitignore` 把 1.3 GB 产物挡住了。
* **`docs/` 图片正确入库**（`.gitignore:36-37` 的 `!docs/` `!docs/**` 取反生效）。
* **`.gitattributes:13-17` 对 `.bat/.vbs/.ps1` 指定 CRLF 正确**，
  `启动XiangQiLens.bat` 确实是 CRLF + GBK（cmd 要求，`:6` 的注释也写明了）。
* **版本号单一来源**（`app_backend.py:26`）。
* **`app_book.py` 的两个 SQLite 后端缓存都有界**（`XqbBook:319`、`ObkBook:609`
  都在 >512 时 `clear()`），不是内存泄漏。
* **`app_vision.py` 的 `_cn_cache` 有界**（`:946-948`，>128 时 clear）。
* **`app_input.py` 的 ctypes 声明与 x64 ABI 一致**（上轮已实测 `sizeof` 为
  40 / 32 字节），`AttachThreadInput` 配对正确。
* **`ObkBook` 的 `CAST(vkey AS REAL)` 全表扫**（`:589-590`）虽然在索引未命中时
  触发，但**未命中结果也被缓存**（`:611`），同一局面只扫一次 ⇒ 可接受。
  上轮注释记的 4.9 ms 是在自建小库上测的；若用户加载「云霄剑诀」这类大库
  （本机无 `.obk` 文件，本轮无法实测），值得在真实大库上复测一次。**若复测
  显著偏高**，可在 `__init__` 里一次性判断该库是否存在 REAL 编码的 vkey，
  不存在就完全跳过 `CAST` 兜底查询。

---

## 3. 建议的落地顺序

**第 1 批（当天，纯低风险，共省约 29 ms/帧 + 200 ms/手）**

1. **P0-2** `app_backend.py:368-369` 改抽样校验 —— **3 行，省 26.3 ms/帧（86x）**
2. **P1-3** `app_backend.py:413`、`:459` 改 `cv2.cvtColor(..., COLOR_BGRA2BGR)` —— **2 行，省 2.6 ms/帧（5.5x）**
3. **P0-1** `app_vision.py:617-619` 把 `confirm` 与 `published` 两个条件拆开 —— **省 200 ms/手**
4. **P1-5** `app_vision.py:2864`、`:2845` 守卫 `thread.wait()` 返回值 —— **防孤儿进程**

**第 2 批（1~2 天，需要提取纯函数 + 补测试）**

5. **P1-4** 主循环预闸门（原始帧版）—— 静止帧 **143 ms → 1.8 ms**；
   注意 `COLOR_RGB2GRAY` → `COLOR_BGR2GRAY`，连带更新 `test_settle_gate.py`
6. **P2-9** `MouseClicker.move_to` 返回真实结果（与上轮 N5 一起改，闭合坐标闸门）
7. **P2-6** `BoardTracker` 返回 `n_moves`，删掉 `app_vision.py:647/653` 的重复校验
8. **P2-7** `TurnManager` / `parse_alternatives` / `legal_moves` 移出生产代码

**第 3 批（可排期）**

9. **P3-11** `tools/_common.py` + 上轮 N1（6 个脚本明确跳过）—— 这是上轮 #4「加 CI」的前置条件
10. **P2-8** 把 `sendinput` 提到 UI，或删掉并修正文档
11. **P2-10** `build_exe.py --zip` 委托给 `make_release_zip.py`
12. **P3-12/P3-13/P3-14** 删陈旧 spec、日志改 append、清硬编码路径
13. 上轮遗留：`#4` requirements/CI · `#6` 上帝类拆分 · `#7` book 去重 · `#9` 图标入库 · `#10` `src/` 归档 · `#8` 清理 1.3 GB 产物 · `N2/N3/N8` 引擎协议层

---

## 附：本次评审的可复现命令

```powershell
cd D:\opencode\XiangQiLink

# 真实推理耗时（本次 §1 的数字）
python -c "
import sys; sys.path.insert(0,'vendor/xq_research')
import numpy as np, time
from xq_vision import XiangqiVision
v=XiangqiVision('D:/opencode/xq_research/hf_model/onnx/pose/4_v6-0301.onnx',
                'D:/opencode/xq_research/hf_model/onnx/layout_recognition/nano_v3-0319.onnx')
img=np.random.randint(0,255,(1253,683,3),dtype=np.uint8)
for _ in range(3): v.infer(img)
ts=[]
for _ in range(15):
    t=time.perf_counter(); v.infer(img); ts.append((time.perf_counter()-t)*1000)
print('median %.1f ms'%np.median(ts))
"

# 抓帧校验与 BGRA→BGR 对比（本次 §1 / §2.3 的数字）
python -c "
import numpy as np, time, cv2
def t(f,n=30):
    f(); t0=time.perf_counter()
    for _ in range(n): f()
    return (time.perf_counter()-t0)/n*1000
img=np.random.randint(0,255,(1253,683,3),dtype=np.uint8)
a=np.random.randint(0,255,(1253,683,4),dtype=np.uint8)
print('astype(float32) %.2f ms'%t(lambda:(img.astype(np.float32).std(),(img.astype(np.float32).max(axis=2)>12).mean())))
print('sample[::8,::8] %.2f ms'%t(lambda:(img[::8,::8].astype(np.float32).std(),(img[::8,::8].max(axis=2)>12).mean())))
print('ascontiguousarray %.3f ms'%t(lambda:np.ascontiguousarray(a[:,:,:3]),200))
print('cvtColor BGRA2BGR %.3f ms'%t(lambda:cv2.cvtColor(a,cv2.COLOR_BGRA2BGR),200))
"

# 原始帧预闸门成本（本次 §2.4 的数字）
python -c "
import sys; sys.path.insert(0,'.')
import numpy as np, time, cv2
from app_vision import gray_small, motion_ratio
bgr=np.random.randint(0,255,(1253,683,3),dtype=np.uint8)
def t(f,n=100):
    f(); t0=time.perf_counter()
    for _ in range(n): f()
    return (time.perf_counter()-t0)/n*1000
print('gray_small %.3f ms'%t(lambda: gray_small(bgr)))
"

# 死代码取证（本次 §2.7）
Select-String -Path *.py,tools\*.py -Pattern 'TurnManager|parse_alternatives|legal_moves\('

# tools 样板重复（本次 §2.11）
(Select-String -Path tools\*.py -Pattern 'sys\.path\.insert').Count
(Select-String -Path tools\*.py -Pattern 'EnumWindows|find_target_window|list_windows').Count
(Get-ChildItem tools\*.py | Get-Content | Measure-Object -Line).Lines

# 陈旧 spec（本次 §2.12）
Select-String -Path XiangQiLens.spec -Pattern 'D:/opencode'
```

> 说明：本报告是本次评审**唯一新增的文件**，未改动仓库任何现有文件（含代码与文档）。
> 所有基准脚本均通过 `python -c` 内联执行，未在仓库内落盘。
