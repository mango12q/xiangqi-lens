# XiangQiLens 优化评审（第四轮）

**日期**：2026-10-08
**基线**：`main` @ `9d31c1d`（v0.5.2 之后；工作区另有未提交的「我方执子」重构与 `tools/book_bench/` 评测工具）
**上一轮**：`optimization-review-round3-2026-10-06.md`
**范围**：`D:\opencode\XiangQiLink` 全仓库
**方法**：真实微基准（本机 DirectML 真跑模型 + 真实对局截图）+ ONNX Runtime profiling + 逐行读码
**证据标注**：【实测】= 本次在本机真实运行得到；【读码】= 逐行核对源码；【引用】= 来自 `开局库评测报告.md`（作者自测，本次核对代码路径）。

---

## 0. 结论速览

### 0.1 本轮最重要的一条：推理慢了 13~24 倍，原因是**输入维度是动态的**

前四轮评审都把「推理 ~116ms」当成不可压缩的物理成本（第三轮 §1 的原话是
「真实热点是 `astype` 26 ms 和推理 116 ms」）。**这个前提是错的。**

两个 ONNX 模型的输入维度声明里带 `batch`/`height`/`width` 动态轴：

```
pose: [('input', ['batch', 3, 256, 256], 'tensor(float)')]     ← 只有 batch 是动态
cls : [('input', ['batch', 3, 'height', 'width'], 'tensor(float)')]
```

应用每帧喂进去的尺寸其实是**恒定**的（`Pose4Kpt` 把整图 `warpAffine` 到 256×256，
`BoardClassifier` 把棋盘 `cv2.resize` 到 280×315）。用
`SessionOptions.add_free_dimension_override_by_name()` 把这三个轴钉死：

| 模型 | 现状（动态维度） | 固定维度后 | 加速 | 输出差异 |
|---|---|---|---|---|
| `cls`（90 格分类） | **124.45 ms** | **5.09 ms** | **24.4x** | 最大绝对差 2.4e-7，argmax 100% 一致 |
| `pose`（4 角点） | 2.29 ms | 1.03 ms | 2.2x | 最大绝对差 4.5e-8，argmax 100% 一致 |
| `XiangqiVision.infer` 整体（真实截图 1833×1256） | **121.6 / 123.3 / 123.9 ms** | **10.1 / 8.7 / 8.8 ms** | **≈13x** | 三张真实截图的 `rows` **逐格完全相同** |

CPU 版同样受益（180.6 ms → 50.7 ms，3.6x），但 DirectML + 固定维度仍是最优（5.6 ms）。

**单帧预算因此改变量级**：

| 阶段 | 现状 | 落地本轮 4 项后 |
|---|---|---|
| `_print_window`（含 BGRA→BGR） | 16.0 ms | ~12.6 ms |
| 抓帧校验 `astype(np.float32)` | 25~30 ms | 0.35 ms（§2.1） |
| `cvtColor` BGR2RGB | 0.7 ms | 0.7 ms |
| `infer`（pose + cls） | **122 ms** | **8.8 ms（§1）** |
| **合计** | **≈165 ms（约 6 Hz）** | **≈22.5 ms（约 44 Hz）** |

### 0.2 本轮条目总表

| # | 优先级 | 问题 | 位置 | 证据 | 实测影响 |
|---|---|---|---|---|---|
| **N1** | **P0** | 模型输入维度是动态的，DML 走慢路径 | `vendor/xq_research/xq_vision.py:53-61`、`:101`、`:143` | 【实测】 | **infer 122 ms → 8.8 ms（13x）**，输出逐格一致 |
| **N2** | **P0** | 抓帧校验整幅 `astype(np.float32)`（三轮未落地） | `app_backend.py:368` | 【实测】 | 25.1 ms → 0.35 ms（86x） |
| **N3** | **P1** | BGRA→BGR 花式索引（三轮未落地） | `app_backend.py:413`、`:459` | 【实测】 | 3.24 ms → 0.52 ms（6.2x） |
| **N4** | **P1** | `confirm` 分支两次 `sleep(0.10)`（三轮未落地） | `app_vision.py:674`、`:688` | 【读码】+【引用】 | **+200 ms/手** |
| **N5** | **P1** | 静止帧仍全量推理（三轮未落地） | `app_vision.py:573` 早于 `:405-409` | 【实测】 | 静止帧 165 ms → ~2 ms |
| **N6** | **P1** | 开局库：索引漏行时**不兜底**，静默丢一半数据 | `app_book.py:641-646` | 【引用】+【读码】 | 屠龙库丢 50.5% 局面、初始局面查不到 |
| **N7** | **P1** | 开局库：着法字节序反了不报错，静默全废 | `app_book.py:525-557`、`:650` | 【引用】+【读码】 | 先锋无敌库「装了等于没装」 |
| **N8** | **P2** | 开局库：脏权重导致固定走坏棋 | `app_book.py:258-269` | 【引用】 | 屠龙库执红第一手恒走「仕六进五」（−41 厘兵） |
| **N9** | **P2** | 发布 zip 把 `.dll/.pyd` 当已压缩格式存 | `tools/make_release_zip.py:53` | 【实测】 | 每次发布多传 **~197 MB**（426.6 MB → ~230 MB） |
| **N10** | **P2** | README 把两项未落地的优化写成「已落地」 | `README.md:239-243` | 【实测】 | 与 `RELEASE_v0.5.2.md:126-128`、代码三方矛盾 |
| **N11** | **P2** | `run_tests.py` 没有通过数下限 | `tools/run_tests.py:107-124` | 【实测】 | 全部跳过也打印「全部通过」并 `return 0` |
| **N12** | **P3** | `_fill_pv_table` 每次刷新重建全部表格项 | `app_vision.py:2681-2698` | 【读码】 | 20 Hz × N 条 PV 的对象分配与选择态丢失 |
| **N13** | **P3** | `verify_release.py` 依赖的 `requests` 未声明 | `requirements-dev.txt` | 【实测】 | 干净环境发布自检直接 `ModuleNotFoundError` |
| **N14** | **P3** | spec 清理写在构建**之前**，等于没清 | `tools/build_exe.py:193-198` | 【实测】 | 工作区现存含绝对路径的 `XiangQiLens.spec` |
| **N15** | **P3** | 未跟踪且未忽略的生成物 ~15.8 MB | 仓库根 / `tools/book_bench/out/` | 【实测】 | `git add -A` 一次不可逆 |

### 0.3 本轮**否定**的两条建议（避免做白工）

| 曾经的猜想 | 本轮实测结论 |
|---|---|
| 第三轮 §2.3 附注：缓存 GDI 对象（DC + 位图）可省每帧 3.4 MB 分配 | **只值 ~0.3 ms**。`_print_window` 16.0 ms 的分解里，`CreateCompatibleDC` 0.08 + `CreateCompatibleBitmap` 0.21 + cleanup 0.04 = **0.33 ms**；大头是 `PrintWindow` 本身 10.13 ms 与 `GetBitmapBits` 1.49 ms。**不值得做。** |
| 「棋盘分类输入 280×315 太小，放大输入可以免费提精度」（因为推理耗时与输入尺寸无关） | **反了，而且很危险**。同一张真实棋盘：280×315 最低置信 **0.804**、识别结果正常；420×473 掉到 **0.223**、560×630 **0.223**、840×945 **0.215**，`rows` 直接变成 `['xxbakab..', ...]` 这种含非法字符的垃圾。**280×315 是正确取值，不要动。** |

---

### 0.4 落地记录（本次已实施：零行为变化档 N1~N4 + 等价性回归测试）

**已改**：

| 项 | 文件 | 改动 |
|---|---|---|
| N1 | `vendor/xq_research/xq_vision.py` | 新增 `make_session(..., fixed_dims=...)` 与 `assert_input_shape()`；`Pose4Kpt` / `BoardClassifier` 构造时钉死维度 |
| N2 | `app_backend.py:368` | 抓帧校验改 `img[::8, ::8]` 抽样 |
| N3 | `app_backend.py:413`、`:459` | 改 `cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)` |
| N4 | `app_vision.py:673-680` | `confirm` 与 `published` 两个条件拆开，前者 `sleep(0.005)` |
| — | `tools/test_session_equiv.py` | **新增**：8 项断言锁住「形状已钉死 + 结果等价 + 错配硬失败」 |

**验证结果**：

* `python app_vision.py --selftest` → `[通过] 全链路可用 · 识别 22 ms/帧`（原 ~170ms），
  FEN 与 `minConf`（0.81~0.82）与改动前一致；
* `python tools/test_session_equiv.py` → **通过 8 / 失败 0**，其中
  「29 张真实截图的 `rows` 全部相同」；
* `python tools/run_tests.py` → **通过 23 / 跳过 6 / 失败 0**（29 个脚本，原 22/6/0）。

**等价性证据汇总**（回答「会不会影响准确性」）：

| 验证对象 | 规模 | 结果 |
|---|---|---|
| cls 输出 | 300 组输入（随机 / 近全黑 / 高对比 / 低对比） | argmax 不一致 **0**；最大绝对差 1.2e-6 |
| pose 输出 | 100 组输入 | argmax 不一致 **0**；最大绝对差 1.6e-7 |
| 真实截图全链路 | 37 张（`shots/` + `probe_out/`） | `rows` **37/37 相同**；置信度最大差 1.0e-6 |
| CPU 版（不同 kernel 集） | 20 组输入 | argmax 不一致 **0**；最大绝对差 7.5e-7 |
| 尺寸错配 | 喂 (1,3,320,288) / batch=2 | **抛 `InvalidArgument`**（硬失败，不是静默算错） |
| BGRA→BGR 改动 | 683×1253 / 1307×778 | **逐字节相同**（最大差 0） |
| 抓帧校验抽样 | 40 张真实截图 + 3 张合成极端帧 | 回退判定 **0 张不一致**；真实图无 std∈(2,5) 的临界帧 |

**诚实边界**：固定维度后输出**不是逐位相同**，存在 1e-6 量级浮点噪声
（kernel 归约顺序不同）。决策量是 argmax，而实测最低置信 0.22~0.80 —— 离
1e-6 能翻转的边界差五个数量级。另外新增了一个失败模式：**覆盖值与实际喂入尺寸
不符**。ORT 对此抛 `InvalidArgument`，且 `assert_input_shape()` 会在**构造期**
就报错，所以最坏表现是「一启动就报错」而不是「悄悄识别错」。

---

## 1.【N1 / P0】动态输入维度：推理慢 13~24 倍（本轮唯一的高量级发现）

### 1.1 现场

`vendor/xq_research/xq_vision.py:53-61`：

```python
def make_session(path: str, prefer_cuda: bool = True) -> ort.InferenceSession:
    avail = ort.get_available_providers()
    providers = []
    if prefer_cuda and "CUDAExecutionProvider" in avail:
        providers.append("CUDAExecutionProvider")
    if "DmlExecutionProvider" in avail:
        providers.append("DmlExecutionProvider")
    providers.append("CPUExecutionProvider")
    return ort.InferenceSession(path, providers=providers)   # ← 没有 SessionOptions
```

`make_session` **完全没有 `SessionOptions`**，于是模型自带的动态轴原样生效。

### 1.2 怎么发现的

`infer` 各段拆开后，**90 格分类占了 95%**：

```
683x1253   infer 117.4 ms  ├ pose.predict 3.31 ms (2.8%) ├ extract_board 0.13 ms └ cls.predict 111.35 ms (94.9%)
1307x778   infer 150.0 ms  ├ pose.predict 3.33 ms (2.2%) ├ extract_board 0.13 ms └ cls.predict 122.88 ms (81.9%)
```

接着做「输入尺寸敏感性」实验（输入越大，若内部 Resize 是瓶颈则应更快）：

```
DML 输入 280x315   108.6 ms
DML 输入 560x630   108.5 ms      ← 与尺寸无关
DML 输入 840x945   106.8 ms
DML 输入 1120x1260 123.9 ms
```

耗时与输入尺寸**无关** ⇒ 不是像素量问题。于是转向「图优化级别」实验，其中
`free_dimension_override` 那一档把耗时从 126 ms 打到 **5.11 ms**：

```
DML DISABLE_ALL / ENABLE_BASIC / ENABLE_EXTENDED / ENABLE_ALL(默认)  123.2 / 130.5 / 125.4 / 126.2 ms
DML free_dimension_override(batch,height,width)                        5.11 ms   ← ★
DML execution_mode=ORT_PARALLEL / mem_pattern=False / arena=False    122.7 / 126.4 / 124.8 ms
CPU (默认)                                                            183.5 ms
```

顺带修正了 profiling 的一个误读：ORT profiler 把 `/backbone/Resize` 记为
「3 次推理累计 653.7 ms 且落在 CPUExecutionProvider」——但同一模型 CPU-only 整体只要
183 ms，**单节点不可能超过整体**。DML 是异步执行，节点级 `dur` 含 fence 等待，
**不能当真实耗时用**。这条也解释了为什么前三轮没人往这个方向查。

### 1.3 改法（约 10 行）

```python
def make_session(path, prefer_cuda=True, fixed_dims=None):
    so = ort.SessionOptions()
    for name, value in (fixed_dims or {}).items():
        so.add_free_dimension_override_by_name(name, value)
    avail = ort.get_available_providers()
    providers = [...]                      # 不变
    return ort.InferenceSession(path, so, providers=providers)
```

两个调用点传**它们本来就会喂的尺寸**（这样「覆盖值 == 实际值」由构造保证）：

* `Pose4Kpt.__init__`（`:101`）：`{"batch": 1}`（该模型 H/W 本就是静态 256×256）
* `BoardClassifier.__init__`（`:143`）：`{"batch": 1, "height": self.input_size[1], "width": self.input_size[0]}`

**落地要点**：

1. 覆盖值必须与实际输入**严格相等**，否则 ORT 会报错或算错。两个 `predict` 里
   分别有 `cv2.warpAffine(..., self.size)`（`:113`）与 `cv2.resize(board_rgb, self.input_size)`（`:149`）
   保证尺寸恒定，但建议加一句 `assert`，把这个不变量写进代码。
2. `app_vision.py:493-501` 的预热用 256×256 假图走完整 `infer`，两个模型拿到的
   尺寸与生产一致 ⇒ **预热逻辑无需改动**，且固定维度后预热也更快。
3. 若将来换模型（输入尺寸不同），`fixed_dims` 要跟着改；可在 `__init__` 里从
   `sess.get_inputs()[0].shape` 读回静态轴做交叉校验，避免静默错配。

### 1.4 风险

**低**。输出已验证逐元素等价（`cls` 最大差 2.4e-7、`pose` 4.5e-8，argmax 全一致），
三张真实截图的 `rows` 完全相同。唯一新增的失败模式是「覆盖值与实际输入不符」，
用 `assert` 即可闭合。

---

## 2. 三轮未落地的四项（本轮补实测数字）

### 2.1【N2 / P0】抓帧校验 `astype(np.float32)`

`app_backend.py:366-371`（`ScreenSource.grab()`，**主循环每帧都走**）：

```python
366  img = self._print_window()
367  if img is not None:
368      a = img.astype(np.float32)                       # ← 整幅转 float32
369      if float(a.std()) > 3.0 and float((a.max(axis=2) > 12).mean()) > 0.02:
```

本轮实测（判据语义不变的 `img[::8, ::8]` 抽样版）：

| 分辨率 | 现状 | 抽样版 | 加速 |
|---|---|---|---|
| 683×1253 | **25.09 ms** | **0.35 ms** | 71x |
| 1307×778 | **29.82 ms** | **0.41 ms** | 73x |
| 2560×1440 | **110.34 ms** | **1.84 ms** | 60x |

判据是「不是全黑」，抽样 1/64 的像素统计 `std` 与亮像素占比**语义完全不变**。
**3 行改动，全仓库性价比最高的单点。**

### 2.2【N3 / P1】BGRA→BGR 花式索引

`app_backend.py:413`（`_print_window`）与 `:459`（`_screen_region`）都是
`np.ascontiguousarray(arr[:, :, :3])`。本轮实测：

| 分辨率 | 花式索引 | `cv2.cvtColor(..., COLOR_BGRA2BGR)` | 加速 |
|---|---|---|---|
| 683×1253 | 3.24 ms | 0.52 ms | 6.2x |
| 1307×778 | 3.87 ms | 0.71 ms | 5.5x |
| 2560×1440 | 13.37 ms | 1.31 ms | 10.2x |

`cv2` 已在 `app_backend.py:21` 导入，**每处 1 行**。

### 2.3【N4 / P1】`confirm` 分支的两次 `sleep(0.10)`

`app_vision.py:668-675`：

```python
668  # 连续 N 帧一致才进入演化校验（先滤掉动画/过渡帧）
669  if key == self._last_key:
670      self._same += 1
671  else:
672      self._last_key, self._same = key, 1
673  if self._same < self.confirm or key == self._published:
674      time.sleep(0.10)          # ← 两个条件共用一次 sleep
675      continue
```

`key != _last_key`（对手每走一步都会触发）时 `_same=1`，要连续 3 帧相同才放行，
**其中两次 sleep 共 200 ms 是纯加时** —— 循环已被推理自然限速，这里的 sleep 只把
反应时间往后推。`app_vision.py:688`（演化校验挡下）也是同一写法。

**改法**：把两个条件拆开，只对「局面没变」（`key == self._published`）保留节流：

```python
if self._same < self.confirm:
    time.sleep(0.005)            # 只为让出 GIL
    continue
if key == self._published:
    time.sleep(0.10)             # 稳态轮询，这里才需要降速
    continue
```

配合 §1 的推理提速，**「对手走子 → 我方落子」的感知延迟**会从
「547 ms 识别确认 + 1200 ms 思考」降到「约 30 ms 确认 + 1200 ms 思考」。

### 2.4【N5 / P1】静止帧仍全量推理 —— **本次刻意未做**

`app_vision.py:565` 抓帧 → `:573` `infer` → `:597` 几何校验 → `:614` 才轮到动画抑制。
**闸门在推理之后**，所以画面完全静止时每帧仍要付全部成本。

第三轮已实测预闸门（原始 BGR 帧）成本，本轮复核：`gray_small` **0.84 ms** +
`motion_ratio` **0.039 ms** ≈ **0.88 ms**。

| 阶段 | 现状 | 加原始帧预闸门 |
|---|---|---|
| 抓帧 + 校验 + 推理 | ≈165 ms（N1~N4 落地后 ≈22 ms） | **≈2 ms**（跳过推理） |

> ★ **这一项与 N1~N4 有本质区别：它会改变识别行为，不是纯提速。**
>
> N1~N4 是「同样的输入 → 同样的输出，只是更快」（§0.4 已逐项实测等价）；
> 而预闸门是**用运动启发式决定「这一帧要不要看」**。如果盘面发生了真实但幅度小的
> 变化（低对比度棋子落子、很轻的过渡动画），变化像素占比低于 `motion_thresh`
> 就会被判为「静止」而**跳过推理 ⇒ 漏掉一次走子**。这是**准确性/鲁棒性上的取舍**，
> 不是免费的。
>
> 因此本轮**按用户要求只做零行为变化档，未实施 N5**。将来要做的话必须配：
> ① 保守阈值（宁可多推理几帧）；② 「最长 X ms 必须强制重识别一次」的兜底；
> ③ 连带更新 `tools/test_settle_gate.py`，并补一条「静止期间盘面真变了也能被识别」
> 的测试。**不要把它和 N1~N4 捆在一起上。**

**落地注意**：`gray_small`（`app_vision.py:71-74`）现在写死 `COLOR_RGB2GRAY`，
因为它的输入一直是推理产出的 RGB 拉正图；挪到原始 BGR 帧必须换 `COLOR_BGR2GRAY`，
否则灰度系数错配、阈值系统性偏移。它是 `tools/test_settle_gate.py` 的被测对象，
改动要连带更新测试。建议抽成纯函数（`raw_frame_moving(prev_gray, bgr, thresh)`）
以便单测，而不是继续往 `run()` 里塞。

---

## 3. 开局库：应用层三个缺陷（`开局库评测报告.md` 的代码侧修复）

`开局库评测报告.md` 用四个真实商业库（云霄剑诀 8.5 MB / 先锋无敌 31.3 MB /
屠龙 181.6 MB / 静香 13.4 MB）做了完整评测，结论是**问题全在编码与索引，数据本身干净**。
报告给出的解法是「生成修好的副本放 `开局库/_fixed/`，让用户在界面上手选」——
**应用层完全没有兜底**。以下三条是应用侧可以做的。

### 3.1【N6 / P1】索引漏行时不兜底 ⇒ 静默丢一半数据

`app_book.py:639-648`：

```python
639  try:
640      # ① 走索引的正常路径（键存成 INTEGER 时命中）
641      rows = self.conn.execute(sql.format("vkey = ?"), (signed,)).fetchall()
642      if not rows:
643          # ② 兜底：键被生成工具按 double 存了 → 必须 CAST 全表扫
644          rows = self.conn.execute(
645              sql.format("CAST(vkey AS REAL) = ?"), (dbl,)).fetchall()
646  except Exception:
647      rows = []
```

兜底条件是 **`not rows`（索引查到 0 行）**。但实测三个库的 `idxkey` 是**坏的**
（`PRAGMA integrity_check` 报 `wrong # of entries in index`），索引漏掉的行只要
「① 不是 0 行」就**永远找不回来**：

| 库 | 库真实合法着法 | 应用实际拿到 | 丢失 | 应用查询延迟 |
|---|---|---|---|---|
| 云霄剑诀7.5 | 172 | 171 | 0.6% | 5.4 ms |
| 先锋无敌nn库241111 | 287 | **0** | **100%** | 18.6 ms |
| 最新屠龙商业库 | 396 | 196 | **50.5%** | **174.8 ms** |
| 静香库 | 80 | 67 | 16.3% | 17.1 ms |

**典型案例**：屠龙库的**初始局面**真实存在 32 条记录，但索引查 0 条、兜底 SQL 也查
0 条 ⇒ 应用在第一步就查不到库。

**改法（两选一，建议都做）**：

1. **兜底判据改成「索引结果里没有合法着法」**：`pick()` 拿到 `cands` 后用
   `legal_check` 过滤，若过滤后为空而 `probe` 走的是索引路径，就再走一次兜底查询
   并重新过滤。这能救回云霄剑诀/静香的全部丢失项与屠龙的一部分。
2. **开库时一次性判定索引是否可信**，不可信就直接走全表扫路径（并给用户提示）。
   判据可以是 `SELECT COUNT(*) FROM bhobk` 与 `SELECT COUNT(*) FROM bhobk INDEXED BY idxkey`
   的对比，或直接 `PRAGMA integrity_check`（181 MB 库约数秒，可在 Worker 线程做）。

### 3.2【N7 / P1】着法字节序反了不报错

`obk_decode_move`（`app_book.py:525-557`）按 `(起点<<8)|终点` 解码。实测「先锋无敌」
存成了 `(终点<<8)|起点`：

| 库 | 正常解码合法率 | 字节交换后合法率 |
|---|---|---|
| 云霄剑诀7.5 | 100% (627/627) | 0% |
| **先锋无敌nn库241111** | **0% (0/296)** | **100% (296/296)** |
| 最新屠龙商业库 | 100% (264/264) | 0% |
| 静香库 | 100% (277/277) | 0% |

后果：`pick()` 用 `legal_check` 把着法全过滤掉 ⇒ **永远返回 `None`，用户以为库在工作，
实际一手都不走**（600 个局面实测：287 个合法库着一个都拿不到，还返回 287 个非法着法）。

**改法**：在 `ObkBook.__init__` 里做一次**自检**（约 20 行）：取若干个已知局面
（初始局面 + 库自身的少量高频键），分别按两种字节序解码并用
`is_valid_iccs_move` 校验，合法率高的那种即正确字节序；把它记成
`self._swap_move`，在 `probe` 里按需交换。**自检结果只在内存里生效，不改用户文件。**
判定不出来（两个都 0 或都满）就保持现状并在 `describe()` 里提示。

### 3.3【N8 / P2】脏权重导致固定走坏棋

`_weight_of`（`app_book.py:258-269`）是 `win*2 + draw`，都为 0 时退到 `score`。
屠龙库初始局面 32 条记录里，`f0e1`（仕六进五）的 `vwin` 是 **230827**，其余全是 1
⇒ 权重 461654 vs 2，`pick` 必然选它，**执红每一局第一手都走「仕六进五」，比引擎最佳差 41 厘兵**。

**改法（保守版）**：权重明显退化时**不采信库着**。判据例如「最高权重 > 次高权重 × 50」
（离群）或「所有候选权重完全相同」（等于随机挑，屠龙有 31.6% 的多候选局面属于此类）。
退化时可以让 `pick` 返回 `None`，交回引擎 —— 报告 §6.3 的实测结论正好支持这个取舍：
**库命中带来的收益是「每局零点几秒」，而库命中一手坏棋要用局面去还**。

### 3.4 顺带：`CAST(vkey AS REAL)` 全表扫（第三轮遗留）

`app_book.py:645` 的兜底查询在屠龙库上实测 **174.8 ms/次**（报告 §7 用表达式索引
`idx_real(CAST(vkey AS REAL))` 修好后降到 0.049 ms，约 3500x）。应用侧的可选改法：
开库时检测「是否存在 REAL 编码的 vkey」（一次全表扫），**存在就提示用户用修好的副本**
或把结果按局面缓存（现状 `_CACHE_MAX=512`，同一局面只扫一次，可接受）。

---

## 4. 工程面（本轮由子代理只读审计 + 我复核关键数字）

### 4.1【N9 / P2】发布 zip 把 `.dll/.pyd` 当已压缩格式，白丢 ~197 MB

`tools/make_release_zip.py:51-53`：

```python
51  # 已经压过的格式（模型、权重）用低压缩级别，省时间；
52  # 文本与二进制程序用高压缩级别。
53  LOW = {".onnx", ".nnue", ".7z", ".zip", ".png", ".jpg", ".pyd", ".dll"}
```

`.onnx/.nnue/.zip` 确实是已压缩格式（STORED 正确），但 **`.dll/.pyd` 是原始 PE 二进制**。
我读 `dist/XiangQiLens-v0.5.2.zip` 的中央目录复核：

```
zip 文件大小 426.56 MB
STORED 合计 407.3 MB  其中 .dll 175.2 MB + .pyd 142.5 MB = 317.7 MB（占 STORED 的 78%）
DEFLATED 合计 31.5 MB
```

子代理实测把这 116 个 `.dll/.pyd`（317.7 MB）用 level 6 压到 121.2 MB（压缩率 38.1%），
**可省约 196.6 MB**：`cv2.pyd` 82.3→29.4 MB、`opencv_videoio_ffmpeg500_64.dll` 29.4→12.4 MB、
`onnxruntime_pybind11_state.pyd` 25.3→7.8 MB。

**改法**：从 `LOW` 里删掉 `.pyd`、`.dll`（1 行）。建议在 `tools/test_build_chain.py`
补一条断言锁住压缩策略（当前只测了「委托给了 make_release_zip.py」）。

### 4.2【N10 / P2】README 把未落地的优化写成「已落地」

`README.md:239-243`（**已在 HEAD 里，不是工作区未提交内容** —— 我用
`git show HEAD:README.md` 确认过）：

> 抓帧校验曾经是大头（… 实测 683×1253 要 26.3ms，占单帧 23%）——**现已改成 8 倍抽样**，
> 同一判据下只要 0.30ms。BGRA→BGR 也**从花式索引改成了 `cv2.cvtColor`**（3.23ms → 0.59ms）。

而代码是 `app_backend.py:368`（整幅 `astype`，未改）与 `:413`（`ascontiguousarray(arr[:,:,:3])`，未改），
`docs/RELEASE_v0.5.2.md:126-128` 也把这两项列在**「本版未包含」**。
**README 说改了、发布说明说没改、代码证明发布说明是对的。**

**影响**：读者（包括未来的维护者）会以为性能问题已解决，从而跳过优化 —— 本轮 §2 的
两项正是因此拖了三轮没落地。**改法**：把该段改成与发布说明一致的「未落地」表述。

### 4.3【N11 / P2】`run_tests.py` 没有通过数下限

`tools/run_tests.py:107-124` 只把 `FAIL/TIMEOUT` 计为坏，**28 个脚本全跳过时照样打印
「全部通过」并 `return 0`**。另外 `:38-39` 的 `discover()` 用 `HERE.glob("test_*.py")`
**不递归**，将来放进子目录的测试会被静默漏跑。

另据子代理核对：CI（无模型/引擎）实际是 **通过 19 / 跳过 9**，而 README 与发布说明
引用的基线是**本机**的 **22 / 6**（本机有模型 + 引擎 + 不入库的 `shots/`）。
**改法**：加 `--min-pass N`（CI 传 19），`discover()` 改 `rglob`，并在文档里把 22/6
标注为「本机（含模型与引擎）」。

### 4.4【N13 / P3】`requests` 未声明

`tools/verify_release.py:13` 与 `tools/verify_repo.py:8` 都 `import requests`，但
`requirements.txt` / `requirements-dev.txt` 都没写（`pyinstaller` 也不传递它）。
`verify_release.py` 是 README 里唯一的「Release 资产真能下载」验证手段，
干净环境跑它只会得到 `ModuleNotFoundError`。**加 1 行。**

### 4.5【N14 / P3】spec 清理写在构建之前

`tools/build_exe.py:193-198` 在 `run(args)` **之前**删 `XiangQiLens.spec`，
而 PyInstaller 构建时会重新生成它 ⇒ 工作区根目录正躺着那个「写着绝对路径」的 spec
（我实测：`pathex=['D:/opencode/XiangQiLink/vendor/xq_research']`），
正是注释想避免的状态。**改法**：移到构建后的 `finally`，或 `--specpath build/`。

### 4.6【N15 / P3】未跟踪且未忽略的生成物

`git status` 显示：`tools/book_bench/`（21 个 `.py` 值得入库，18 个 `out/*.json` 共 5.33 MB
是评测中间产物，最大 `t2_replay.json` 4.90 MB）与 `开局库评测报告.md`（19 KB，人写报告，
建议入库）。`.gitignore` 本轮已加 `/开局库/`，但没覆盖 `tools/book_bench/out/`。

---

## 5. 其它（P3，值得知道但不必马上做）

* **【N12】`_fill_pv_table`**（`app_vision.py:2681-2698`）每次 `diag` 刷新都
  `setRowCount` + 重建 `len(lines) × 6` 个 `QTableWidgetItem`。引擎流式刷新是
  **20 Hz**（`app_vision.py:985` 的 `_last_emit >= 0.05`）⇒ MultiPV=3 时每秒 360 次对象分配，
  且会清掉用户的选中行/滚动位置。改法：行数不变时只 `setText`。
* **`engine_client.py:118`** 用无界 `queue.Queue()`。当前消费速度（每轮循环顶部
  `_drain_lines`）远快于引擎输出，**不构成泄漏**；但 `_stop_search`/重建期间若阻塞较久，
  队列会短暂增长。可设 `maxsize` 并丢弃最旧的 info 行。
* **`vendor/xq_research/engine_client.py:300`** 在 `__main__` 演示块里硬编码
  `D:\opencode\xq_research\pikafish\...`（不进运行路径，但是「模块 demo 硬编码本机路径」的坏范式）。
* **`tools/_common.py` 迁移只完成约 1/5**：66 个 `tools/*.py` 里 50 个仍手写
  `sys.path.insert`，17 个测试脚本一个没改。纯机械替换，`run_tests.py` 可立刻验证。
* **本地产物体积**：实测 **2070.5 MB**（dist 865.4 + `_build_ok` 453.9 + `开局库` 542.0 +
  engines 106.2 + build 46.1 + analysis 40.1 + shots 12.0 + probe_out 4.8），
  全部被 `.gitignore` 挡住 —— 第三轮记的「1.3 GB」已过时。

---

## 6. 建议的落地顺序

**第 1 批（✅ 本次已完成，见 §0.4）**

1. ~~**N1** 固定输入维度~~ → **已落地**（`xq_vision.py`；infer 121.6 → 8.8 ms）
2. ~~**N2** 抓帧校验改抽样~~ → **已落地**（省 25~30 ms/帧）
3. ~~**N3** BGRA→BGR 改 `cv2.cvtColor`~~ → **已落地**（省 2.6~3.4 ms/帧）
4. ~~**N4** 拆开 `confirm` 的两个条件~~ → **已落地**（省 200 ms/手）
5. ~~等价性回归测试~~ → **已落地**（`tools/test_session_equiv.py`，8 项断言）

> 合起来：单帧 ~165 ms → **~22 ms**（约 6 Hz → 约 44 Hz），
> `--selftest` 实测「识别 22 ms/帧」，全量回归 23 通过 / 6 跳过 / 0 失败。

**第 2 批（需先决策，含行为取舍）**

6. **N5** 原始帧预闸门 —— ⚠ **有准确性取舍**（见 §2.4 的风险说明），
   要与 N1~N4 分开评估；必须配保守阈值 + 强制重识别兜底
7. **N6** 开局库兜底判据改成「过滤后无合法着法」
8. **N7** obk 字节序自检（内存内生效，不改用户文件）
9. **N8** 权重退化时不采信库着

**第 3 批（可排期）**

10. **N9** 从 `LOW` 移除 `.pyd/.dll`（下次发布即省 ~197 MB）
11. **N10** README 表述 —— 本轮 N2/N3 落地后，那两处「已改成…」**已经变成事实**；
    本次已顺带补上 N1 的说明与新的单帧数字（原来的 170ms 已过时）
12. **N11** `run_tests.py --min-pass` + `rglob`
13. **N13/N14/N15** `requests` 声明、spec 清理时机、`.gitignore` 补 `tools/book_bench/out/`
14. **N12** PV 表格增量更新；`_common.py` 迁移收尾

---

## 附：可复现命令

```powershell
cd D:\opencode\XiangQiLink

# §1 固定维度 vs 动态维度（含输出等价性校验）—— 本轮核心证据
python -c "
import sys, time, statistics
sys.path.insert(0, 'vendor/xq_research')
import numpy as np, onnxruntime as ort
CLS = r'D:\opencode\xq_research\hf_model\onnx\layout_recognition\nano_v3-0319.onnx'
x = np.random.rand(1,3,315,280).astype(np.float32)
def run(dims=None):
    so = ort.SessionOptions()
    for k,v in (dims or {}).items(): so.add_free_dimension_override_by_name(k,v)
    s = ort.InferenceSession(CLS, so, providers=['DmlExecutionProvider']); n=s.get_inputs()[0].name
    for _ in range(3): s.run(None,{n:x})
    ts=[]
    for _ in range(20):
        t0=time.perf_counter(); s.run(None,{n:x}); ts.append((time.perf_counter()-t0)*1000)
    return s, statistics.median(ts)
sa, ta = run()
sb, tb = run({'batch':1,'height':315,'width':280})
oa = sa.run(None,{sa.get_inputs()[0].name:x})[0]; ob = sb.run(None,{sb.get_inputs()[0].name:x})[0]
print('动态 %.2f ms  固定 %.2f ms  加速 %.1fx' % (ta, tb, ta/tb))
print('最大绝对差 %.3e  argmax 一致 %s' % (np.abs(oa-ob).max(), (oa.argmax(-1)==ob.argmax(-1)).all()))
"

# §2.1 抓帧校验
python -c "
import numpy as np, time
def t(f,n=10):
    f(); t0=time.perf_counter()
    for _ in range(n): f()
    return (time.perf_counter()-t0)/n*1000
for (w,h) in [(683,1253),(1307,778),(2560,1440)]:
    img=np.random.randint(0,255,(h,w,3),dtype=np.uint8)
    print('%dx%d  astype整幅 %.2f ms  抽样1/8 %.2f ms' % (w,h,
        t(lambda:(img.astype(np.float32).std(),(img.astype(np.float32).max(axis=2)>12).mean())),
        t(lambda:(img[::8,::8].astype(np.float32).std(),(img[::8,::8].astype(np.float32).max(axis=2)>12).mean()),20)))
"

# §2.2 BGRA->BGR
python -c "
import numpy as np, time, cv2
a=np.random.randint(0,255,(1253,683,4),dtype=np.uint8)
def t(f,n=200):
    f(); t0=time.perf_counter()
    for _ in range(n): f()
    return (time.perf_counter()-t0)/n*1000
print('花式索引 %.2f ms   cvtColor %.2f ms' % (t(lambda:np.ascontiguousarray(a[:,:,:3])), t(lambda:cv2.cvtColor(a,cv2.COLOR_BGRA2BGR))))
"

# §4.1 发布 zip 的压缩策略取证
python -c "
import zipfile, collections, os
z = zipfile.ZipFile(r'dist\XiangQiLens-v0.5.2.zip')
st = collections.Counter()
for i in z.infolist():
    if i.compress_type == zipfile.ZIP_STORED:
        st[os.path.splitext(i.filename)[1].lower()] += i.file_size
print({k: '%.1fMB'%(v/2**20) for k,v in st.most_common()})
"

# §4.2 README 与代码的矛盾
git show HEAD:README.md | Select-String '现已改成 8 倍抽样'
Select-String -Path app_backend.py -Pattern 'astype\(np\.float32\)|ascontiguousarray'
```

> 本报告是本次评审**唯一新增的文件**。评测用的临时脚本写在 `%TEMP%`，
> 未在仓库内落盘；profiling 过程中 ORT 在工作区根生成的
> `onnxruntime_profile__*.json`（10.46 MB）已删除。
