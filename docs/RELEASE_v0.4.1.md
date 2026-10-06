# XiangQiLens v0.4.1

修好「目标窗口选错」引发的一连串问题：下拉框里混进系统壳 / 输入法 / 显卡服务的
假窗口，被自动选中后截到别的程序画面，表现为**截图歪斜、棋盘定位失败、自动走棋
坐标全错**。这一版过滤掉这些窗口，并让程序按特征**自动选中真正的棋局窗口**。

![界面](https://raw.githubusercontent.com/mango12q/xiangqi-lens/main/docs/screenshot.png)

---

## 下载与运行

下载 `XiangQiLens-v0.4.1.zip`，解压后双击 **`XiangQiLens.exe`**。

**无需安装 Python 或任何依赖** —— 运行时已打包进 exe。

> ⚠️ 请**解压后整目录使用**，不要只把 exe 单独拷出来。exe 需要同级的
> `xq_research/` 目录提供识别模型与引擎（约 95MB）。

系统要求：Windows 10 / 11（64 位）。

---

## v0.4.1 更新内容

### 修复：目标窗口混入系统窗口，导致截图歪斜

**真实故障**（这一版就是为它而来）：`MS_WebcheckMonitor` 是 explorer.exe 托管的
微信小程序辅助窗口，实测尺寸 **1701×1015**，比真正的小程序窗口
（天天象棋 1307×778）还大。它因此排在下拉框第一位、甚至被自动选中；
而它的 `PrintWindow` 返回全黑 → 抓帧回退到「屏幕区域截图」→ 截到的是被其它
窗口遮挡 / 错位的画面。

后果是连锁的：用户看到的「截屏歪了」→ 棋盘几何校验宽高比 0.74 失败 →
角点定位失败 → 自动走棋坐标全错。

#### 改动 1：候选窗口过滤（`app_backend.py`）

新增 `is_candidate_window()`，在枚举窗口时剔除四类噪声：

| 判据 | 内容 |
|---|---|
| 类名精确黑名单 `_WINDOW_CLASS_DENY` | 桌面 / 任务栏 / 系统壳（`workerw`、`progman`、`shell_traywnd`、`ms_webcheckmonitor`…）+ Win32 通用控件类（`static`、`button`、`tooltips_class32`…） |
| 类名子串 `_WINDOW_CLASS_DENY_SUB` | `trayicon`、`notification`、`atl:`、`uuremoteclass`… |
| 进程名 `_WINDOW_PROC_DENY` | `explorer.exe`、`applicationframehost.exe`、`textinputhost.exe`、NVIDIA Overlay、`rail.exe`、`tcls_core.exe`… |
| 标题 `_WINDOW_TITLE_DENY` | 含 `XiangQiLens` / `WorkBuddy` |

另外按 pid 排除**本程序自身**的窗口 —— 否则自动选择可能选中自己的界面当棋盘。

被过滤的窗口不再出现在下拉框里，用户选不到，自动选择逻辑也不会误中。

#### 改动 2：进程名改用纯 ctypes 获取

新增 `_proc_name(pid)`，走 `kernel32!QueryFullProcessImageNameW`。

**为什么不用 `psutil`**：它是**可选**依赖 —— 既没写进 README 的安装命令，也没进
打包脚本的 `hiddenimports`。一旦缺失（打包发行版尤其容易），进程名会静默返回
空串，于是进程黑名单与自动选择打分里的「微信进程」加分**全部失效而无人察觉**。
系统 API 则源码运行与打包后行为一致，且零三方依赖。

结果按 pid 缓存；缓存超过 512 条时清空（长跑进程里 pid 复用会让缓存无限增长）。

#### 改动 3：打分式自动选中棋局窗口（`app_vision.py`）

原来的自动选中只认标题里的「JJ象棋」，其它平台（天天象棋小程序、网页版）全部漏掉。
改为打分：

| 特征 | 加分 |
|---|---|
| 标题含「象棋」 | +100 |
| 宿主进程是 `WeChatAppEx.exe` / `Weixin.exe` / `WeChat.exe` | +40 |
| 类名以 `Chrome_WidgetWin` 开头（CEF / WebView 渲染窗） | +20 |
| 窗口未最小化 | +10 |

阈值 `WINDOW_SCORE_MIN = 80`：只有「标题里写了象棋」的窗口才会被自动选中；
普通小程序窗口（70 分）、微信主窗口 / 记事本（≤50 分）都不会。一个都不像时
停在第一项并明确提示「请手动选择」，不再让用户误以为已经选好。

下拉框标签现在额外显示**宿主进程名**，便于人工区分：
`天天象棋  [Chrome_WidgetWin_0]  WeChatAppEx.exe  1307x778`。

#### 改动 4：屏幕区域截图新增遮挡保护（`ScreenSource`）

屏幕区域截图抓到的是「该矩形当前显示的东西」——若目标窗口此刻不在最上层，抓到的
就是别的程序的画面。新增 `_window_visible_ratio()`：在窗口矩形内取**中心 + 四角
内缩共 5 点**采样，仍属于目标窗口的比例 < 0.6 时**放弃本次抓帧**（返回 `None`
让上层明确报「无法抓取」），而不是静默产出垃圾图。

> 为什么不只看中心一个点：输入法候选框、tooltip、悬浮球这类小窗正好压在窗口中心时，
> 单点检查会误判「整窗被遮挡」而放弃；5 点取比例则不受少数点被小浮层压住的影响。

### 新增：工具脚本

| 脚本 | 用途 |
|---|---|
| `tools/test_window_filter.py` | 窗口过滤规则单测（黑名单 / 自身窗口 / 保留正常窗口） |
| `tools/test_window_score.py` | 自动选择打分单测（含阈值边界：小程序 170 / JJ 130 / 普通小程序 70） |
| `tools/test_screen_region_guard.py` | 遮挡保护单测（可见比例阈值、退化矩形） |
| `tools/diag_window_chain.py` | 诊断脚本：打印当前枚举到的窗口及各自得分，排查「为何没选中」 |

---

## 与 v0.4.0 的兼容性

- 识别、引擎、棋规、自动走棋逻辑**均无破坏性改动**。
- `app_backend.list_windows_any()` 返回的字典**新增** `pid` / `proc` 两个字段；
  `ScreenSource.list_windows()` 返回值结构不变，但会过滤掉噪声窗口。
- **行为变化（有意）**：`ScreenSource.grab()` 在窗口被遮挡时会返回 `None`，
  同时把 `self.rect` 置 `None`。此前它返回的是被遮挡的错误画面 —— 这是本次修复的核心。
- 版本号：`app_backend.__version__` = `0.4.1`。

---

## 免责声明

本工具仅用于个人学习、复盘与棋艺研究。请在使用时遵守对局平台的用户协议，
不要将其用于任何违反平台规则的场景。是否使用及使用后果由使用者自行承担。

「自动走棋」功能默认关闭，且默认处于预览模式；开启它意味着由程序代替你落子，
请自行确认目标平台是否允许此类操作。
