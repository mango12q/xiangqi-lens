# XiangQiLens v0.2.1

> **本版没有单独发布二进制包** —— 内容已完整包含在 [v0.3.0](https://github.com/mango12q/xiangqi-lens/releases/tag/v0.3.0) 中。
> 此处补建 Release 仅为补齐版本记录。

---

## 修复：朝向判断

v0.2.0 认为「执黑时画面上下颠倒，必须翻转矩阵」，于是按「我方执子」把 90 格矩阵旋转 180°。
**这是错的。**

识别模型的关键点本身就是内容语义的（`xq_vision.BONE_NAMES`：A0/A8 为黑方两角，J0/J8 为红方两角），
透视拉正流程已经把黑方底线摆到 row 0 —— 无论哪一方在画面下方，模型输出都已是标准朝向。

再翻一次会把局面翻反（黑将落到 row 9），被「局面严格校验」判成 `黑将在九宫外 (9,4)`，
每帧都挡下，状态栏只会一直刷「局面变化无法解释，已挡下」。

**现在矩阵不再按「我方执子」翻转**，只在遇到模型输出异常（黑将真的在下方）时自检兜底翻转一次。
「我方执子」现在只影响胜率视角与左栏显示图朝向。

---

## 其它变更

- `README.md` 同步说明「我方执子」与「先手」的区别
- `tools/test_turn.py` 适配内部接口调整（`_fen_rows` → `_rows_of`）
- 启动器 `.vbs` → **`启动XiangQiLens.bat`**（支持 `debug` 参数，前台运行便于看报错）
- 新增 `tools/probe_tracker_branches.py`：验证四道闸门各分支的探针
- `.gitignore` 忽略 `.workbuddy-ai/`

---

完整的功能说明与实测数据见 [v0.3.0 Release](https://github.com/mango12q/xiangqi-lens/releases/tag/v0.3.0)。
