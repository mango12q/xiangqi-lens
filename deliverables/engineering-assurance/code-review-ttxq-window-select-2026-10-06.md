# XiangQiLens v0.4.1 — 天天象棋小程序「截屏歪斜 + 自动走棋失效」修复与代码审查报告

**日期**：2026-10-06
**工作流**：工作流 1（综合代码审查）
**参与成员**：Cody（代码审查师）、Tessa（测试专家）；由 Zhen（工程督导）编排汇编

---

## 📌 TL;DR（执行摘要，3-5 行）

- **根因**：「目标窗口」自动选择只认标题含「JJ象棋」，而天天象棋小程序窗口标题是「天天象棋」，匹配不上，于是落到下拉框第一项 `MS_WebcheckMonitor`（explorer.exe 托管的辅助窗口，1701x1015，比真窗口还大）。该窗口 `PrintWindow` 返回全黑 → 抓帧回退「屏幕区域截图」→ 截到被遮挡/错位的画面 → pose 角点误检 → 棋盘宽高比 0.74（正常 0.889）→ 几何校验失败；自动走棋坐标基于错误透视矩阵，落点全错。
- **修复**：窗口候选四重过滤 + 打分自动选择（阈值 80）+ 屏幕抓帧 5 点遮挡保护；进程名查询改为**零依赖 ctypes** 实现，消除打包后静默失效隐患。
- **严重度分布**：🔴 严重 1 项（已修） / 🟠 高 1 项（已修） / 🟡 中 3 项（2 项已修，1 项判定保留并说明） / 🟢 低 2 项（无需改动）
- **阻塞/非阻塞**：**无阻塞项**。10 个既有测试 + 3 个新增测试（共 51 条断言）全部通过。

---

## 🎯 核心结论卡片

| 项目 | 内容 |
|------|------|
| 整体评级 | 🟢 通过（缺陷已修复并经实测验证） |
| 阻塞项数量 | 0 |
| 关键行动项 | 4 条 |
| 建议下一步 | 真机开一局天天象棋，实测自动走棋实际落子（链路已逐段验证，仅"真点鼠标"环节待真机确认） |

---

## 🔍 一、根因定位（实测证据链）

| # | 环节 | 实测结果 |
|---|------|---------|
| 1 | 枚举窗口 | 天天象棋真实窗口 = `0x0007011E`，标题 `天天象棋`，类名 `Chrome_WidgetWin_0`，进程 `WeChatAppEx.exe`，尺寸 1307x778 |
| 2 | 误选窗口 | `MS_WebcheckMonitor`，类名 `MS_WebcheckMonitor`，**进程 explorer.exe**，尺寸 1701x1015（比真窗口更大，故按面积排序排第一） |
| 3 | 抓帧通道 | 对 `MS_WebcheckMonitor`：`PrintWindow` 返回 `mean=0.0`（**全黑**）→ 触发 fallback 到「屏幕区域截图」 |
| 4 | 屏幕截图 | 该矩形区域实际被其他窗口遮挡/错位 → 截出的棋盘是斜的、被裁切的 |
| 5 | 识别失败 | `几何校验失败: 棋盘宽高比 0.74 超出合理范围 [0.75, 1.3]`（正常 0.889） |
| 6 | 自动走棋 | 坐标换算依赖该帧的透视矩阵，矩阵既错 → 落点全错 → 被"落点在目标窗口上"闸门拦下或点到别处 |
| 7 | 对照 | 对真窗口 `0x0007011E`：`PrintWindow` 干净、宽高比 0.882、90 格识别 100% 正确、FEN 正确 |

**结论**：用户描述的「截屏歪了」「自动走棋用不了」「窗口名字识别成 ms 啥啥啥」三个现象，是**同一个根因**的三种表现。

---

## 🔧 二、修复内容

### `app_backend.py`

| 位置 | 改动 |
|------|------|
| `115` / `134` / `145` | 新增 `_WINDOW_CLASS_DENY`（系统壳 + Win32 通用控件类）、`_WINDOW_CLASS_DENY_SUB`（类名子串特征词）、`_WINDOW_PROC_DENY`（系统/游戏平台/显卡服务进程）、`_WINDOW_TITLE_DENY` |
| `148` | 新增 `_window_pid()` |
| `180` | 重写 `_proc_name()`：由 psutil 改为 **ctypes `QueryFullProcessImageNameW`** + 按 pid 缓存（见审查发现 #1） |
| `215` | 新增 `is_candidate_window()`：按「自身进程 → 类名 → 类名子串 → 进程名 → 标题」四重判据过滤 |
| `255` / `272` | 新增 `_point_belongs_to_window()` / `_window_visible_ratio()`（5 点采样遮挡判定） |
| `315` | `ScreenSource.list_windows()` 接入过滤（返回格式仍是 4 元组，保持既有 tools 脚本兼容） |
| `423` | `ScreenSource._screen_region()` 新增**遮挡保护**：5 点采样命中率 < 0.6 则返回 `None` 并清空 `rect`，不再静默产出错位图 |
| `483` | `list_windows_any()` 接入过滤，并新增返回 `pid` / `proc` 字段 |
| `26` | `__version__` 0.4.0 → **0.4.1** |

### `app_vision.py`

| 位置 | 改动 |
|------|------|
| `1838` | 新增类常量 `WINDOW_SCORE_MIN = 80`（附取值依据注释） |
| `1841` | 新增 `MainWindow._window_score()`：标题含「象棋」+100、微信宿主进程 +40、`Chrome_WidgetWin*` +20、未最小化 +10 |
| `1862` | 重写 `refresh_windows()`：由「只认 JJ象棋」改为**取最高分**；低于阈值不自动选中并明确提示，不再默认停在列表第一项 |

### 其它

- `tools/diag_window_chain.py`（新增）：指定窗口跑「抓帧 → pose 角点 → 90 格 → FEN → 走棋坐标换算」全链路诊断，本次排查的主要工具。
- `tools/test_window_filter.py` / `test_window_score.py` / `test_screen_region_guard.py`（新增）：依 Tessa 的 P0 建议补齐纯函数测试。
- `README.md`：目标窗口一节补充天天象棋说明与"已自动过滤/自动选中"的行为描述。

---

## 🧾 三、审查发现（按严重度排序）

| # | 严重度 | 类别 | 文件:行 | 问题描述 | 处置 | 来源 |
|---|--------|------|---------|---------|------|------|
| 1 | 🔴 严重 | 依赖/健壮性 | `app_backend.py:180`（原 `_proc_name`） | `psutil` 是**未声明**的可选依赖：README 安装命令 `pip install PySide6 opencv-python numpy mss pywin32 cchess` 未包含，`XiangQiLens.spec` 的 `hiddenimports` 也未包含。打包发行版中 `_proc_name` 会**静默返回空串**，导致 `_WINDOW_PROC_DENY` 与打分里的「微信进程 +40」全部失效（死代码），源码运行与打包行为不一致 | ✅ **已修**：改用系统 API `QueryFullProcessImageNameW`（零三方依赖）+ 按 pid 缓存。已加实测断言「能取到自身进程名 / 形如 *.exe」 | Cody |
| 2 | 🟠 高 | 正确性 | `app_vision.py:1838`（原阈值 60） | 阈值 60 偏低：修好 #1 后，任意微信/CEF 窗口即可得 70 分（微信宿主 40 + CEF 20 + 未最小化 10），会被**误自动选中** | ✅ **已修**：阈值上调至 80，效果变为「只有标题里写了象棋的窗口才会被自动选中」；补了边界用例 | Cody |
| 3 | 🟡 中 | 健壮性 | `app_backend.py:423` | 遮挡保护原先只取窗口**中心单点**，输入法候选框 / tooltip / 悬浮球正好压在中心时会误判「整窗被遮挡」而放弃抓帧 | ✅ **已修**：改为中心 + 四角内缩共 5 点采样，命中率 ≥ 0.6 即放行 | Cody |
| 4 | 🟡 中 | 性能 | `app_backend.py:148` | 每次枚举窗口都对每个 hwnd 调 `GetWindowThreadProcessId` + 进程查询，窗口多时有重复开销 | ✅ **已修**：`_proc_name` 加 pid→进程名缓存（上限 512 条，超限清空防 pid 复用导致无限增长） | Cody |
| 5 | 🟡 中 | 边界语义 | `app_backend.py:255` | `WindowFromPoint` 返回 0 时返回 `False`，窗口大幅移出屏幕时可能误判 | ⚪ **保留**：此处 `h==0` 是"该点不属于任何窗口"的**明确判定**（非"判不了"），返回 False 语义正确；异常分支仍返回 True 放行。已在代码注释说明 | Cody（主理人判定） |
| 6 | 🟢 低 | 兼容性 | `app_backend.py:315` | 类名黑名单是否误杀真实棋局窗口 | ✅ **确认无问题**：`EnumWindows` 只枚举顶层窗口，`static`/`twincontrol` 等通用控件类不可能承载棋局主窗口；已补「天天象棋 / JJ象棋 / 中国象棋 / 微信主窗口 / WeGame 均保留」的防误杀断言 | Cody |
| 7 | 🟢 低 | 兼容性 | `app_backend.py:315` | `list_windows` 返回值格式变化会影响既有 tools 脚本 | ✅ **确认无问题**：返回值仍是 4 元组 `(hwnd, title, cls, rect)`，`tools/` 下 10 处调用点解包方式不变 | Cody |

---

## 🧪 四、测试覆盖评估（Tessa）

**改动前覆盖状况**：窗口过滤、打分函数、遮挡保护三块**零自动化覆盖**，此前全靠人工真机验证。

**覆盖矩阵（改动后）**

| 改动点 | 测试 | 状态 |
|--------|------|------|
| `is_candidate_window` 类名判据 | `test_window_filter.py`（11 例） | ✅ 已补 |
| `is_candidate_window` 标题判据 | `test_window_filter.py`（2 例） | ✅ 已补 |
| `is_candidate_window` 进程判据 | `test_window_filter.py`（6 例，monkeypatch） | ✅ 已补 |
| 自身进程剔除 | `test_window_filter.py`（1 例，monkeypatch） | ✅ 已补 |
| 真实棋局窗口防误杀 | `test_window_filter.py`（6 例） | ✅ 已补 |
| `_proc_name` ctypes 实现 | `test_window_filter.py`（4 例，真实调用） | ✅ 已补 |
| `_window_score` 权重 | `test_window_score.py`（7 例） | ✅ 已补 |
| 阈值边界 | `test_window_score.py`（7 例） | ✅ 已补 |
| 遮挡保护 | `test_screen_region_guard.py`（6 例，monkeypatch + 真实抓帧） | ✅ 已补 |
| `_screen_region` rect 对齐 | `test_screen_region_guard.py`（1 例） | ✅ 已补 |
| `refresh_windows` 集成 | 需真实窗口 | ⚠️ 人工验证 |

**测试规模**：既有 10 个测试脚本全通过；新增 3 个脚本共 **51 条断言**全通过。

**建议人工验证步骤（真实窗口依赖部分）**

```bash
# 1) 全链路诊断（自动找标题含「象棋」的窗口）
python tools/diag_window_chain.py

# 2) 验证置前能力（自动走棋前置条件）
python -c "import sys;sys.path.insert(0,'.');from app_backend import enable_dpi_awareness,list_windows_any;from app_input import ensure_foreground;enable_dpi_awareness();h=[x for x in list_windows_any(200) if '天天象棋' in x['title']][0]['hwnd'];print('ensure_foreground:',ensure_foreground(h))"

# 3) 自动走棋预览（不点击，只算坐标）
python tools/test_automove_e2e.py
```

---

## ✅ 五、行动清单（按优先级排序）

| # | 行动 | 负责角色 | 紧急度 | 预期完成 |
|---|------|---------|--------|---------|
| 1 | **真机实测**：开一局天天象棋，勾选「自动走棋（模拟鼠标点击）」→ 先看预览坐标是否落在棋子上 → 取消预览实点一次，确认 JJ 系客户端认「两次点击」还是「拖拽」，并校准 `click_gap`(120ms) / `place_wait`(2.5s) | 用户 + Zhen | P0 | 下一次对局 |
| 2 | 若实测发现天天象棋的落子交互与 JJ象棋不同（如需要 hover 分步移动），调整 `app_base()/automove.json` 的 `move_steps`(4~8) 与 `click_mode` | Zhen | P0 | 实测后 |
| 3 | 补充 `docs/RELEASE_v0.4.1.md` 并在打包前跑一次 `python tools/build_exe.py --zip` 冒烟（**本次未打包**，源码运行验证） | Zhen | P1 | 需要发布时 |
| 4 | 把 `psutil` 从 `tools/probe_windows.py` 的"可选依赖"表述同步为"可缺省"，避免后续再误引入运行时依赖 | Docu | P2 | 下次维护 |

---

## ⚠️ 六、待完善 / 已知局限

- **自动走棋的"真点鼠标"环节仍未真机验证**。本次已逐段验证：抓帧 ✅、识别 ✅、坐标换算 ✅、`ensure_foreground` 置前成功 ✅、落点判定 ✅；唯独"点下去客户端是否认"必须真机对局才能确认（与 v0.4.0 遗留的同一项）。
- **JJ象棋 PC 端的类名未实测**（用户当前只开了天天象棋）。代码层面它不在任何黑名单内，且标题含「象棋」得 130 分会被自动选中；但"过滤后仍能出现在列表里"这一点建议用户下次开 JJ象棋时顺手确认一次。
- **`MS_WebcheckMonitor` 的本质**：它是 explorer.exe 托管、与微信小程序窗口同尺寸的辅助窗口。本次按"进程名 explorer.exe + 类名精确匹配"双重剔除；若微信后续更换宿主实现，可能需要补规则。
- 阈值 80 的代价：**标题不含「象棋」的棋局窗口不会被自动选中**（例如某些网页版棋盘），此时状态栏会提示用户手动选择 —— 这是有意为之的保守取舍。
- 本次仅改动源码并重启验证，**未重新打包 exe**，`dist/` 内仍是 v0.4.0 产物。

---

## 📚 数据来源 & 成员产出索引

- **Cody（代码审查师）** 原始产出：5 条发现（1🔴 / 1🟠 / 3🟡 / 2🟢），结论「需修改后发布（须修 psutil 依赖并同步上调阈值）」。主理人已落实 #1–#4，#5 判定保留并说明。
- **Tessa（测试专家）** 原始产出：覆盖矩阵 + 缺口清单 + P0 测试建议。已按建议实现 3 个测试脚本（51 条断言）。
- **Zhen（工程督导）** 现场实测证据：窗口枚举 / PrintWindow 对比 / 识别结果 / 坐标换算 / `ensure_foreground` 验证。
- 关键代码位置：`app_backend.py:115-300`、`app_backend.py:423-440`、`app_backend.py:483-520`、`app_vision.py:1838-1895`。
- 诊断工具：`tools/diag_window_chain.py`。

---

> 本报告由工程保障团队 AI 协作生成，关键决策请由人类工程负责人复核。
