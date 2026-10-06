# -*- coding: utf-8 -*-
"""引擎生命周期单元测试 —— 不需要游戏窗口、不启动真实引擎、不加载模型。

覆盖三轮评审里「关窗口漏掉 Pikafish 进程」那一类问题的两个结构性修复：

1. ``run()`` 创建引擎后立刻发现已被要求停止（GUI 线程在「加载识别模型
   10~30 秒」期间就调过 ``stop()``）→ 必须自己把引擎收掉，不能留孤儿。
2. ``run()`` 正常/异常退出时自己收掉引擎，不再依赖 GUI 线程在正确的时刻
   调用 ``shutdown()``。

用假的 vision / engine 替换真实实现，因此**秒级完成、无副作用**。
另附运行日志的追加与轮转验证。
"""
from __future__ import annotations

import queue
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import app_vision  # noqa: E402
from app_backend import ScreenSource  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [通过] {name}")
    else:
        FAIL += 1
        print(f"  [失败] {name}  {detail}")


# ---------------------------------------------------------------------------
# 假实现
# ---------------------------------------------------------------------------
class _FakeStdin:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeProc:
    def __init__(self):
        self.stdin = _FakeStdin()

    def poll(self):
        return None                      # 未退出


class FakeEngine:
    """记录 quit() 是否被调用 —— 这就是「引擎有没有被收掉」的判据。"""

    def __init__(self, on_create=None):
        self.quit_called = 0
        self.proc = _FakeProc()
        self.option_lines: list[str] = []
        self.q: queue.Queue = queue.Queue()
        self._on_create = on_create

    # 引擎接口
    def quit(self):
        self.quit_called += 1

    def send(self, cmd):
        pass

    def drain(self):
        pass

    def set_option(self, name, value):
        pass


class FakeVision:
    def infer(self, img):
        raise RuntimeError("假 vision 不做真推理")


def make_worker(hwnd=None):
    src = ScreenSource()
    src.hwnd = hwnd                        # None ⇒ 跳过 window_state 查询
    w = app_vision.Worker(src)
    return w


def _patch(monkeypatch_engine, monkeypatch_vision=True):
    """替换 app_vision 命名空间里的 create_engine / create_vision。"""
    app_vision.create_engine = monkeypatch_engine
    if monkeypatch_vision:
        app_vision.create_vision = lambda cuda=True: FakeVision()


# ---------------------------------------------------------------------------
# 用例 1：创建引擎期间已被要求停止 → 必须就地收掉
# ---------------------------------------------------------------------------
def case_stop_during_create() -> None:
    print()
    print("[1] 启动期间收到停止请求：新建的引擎必须就地关闭（防孤儿进程）")
    created: list[FakeEngine] = []

    def fake_create_engine(threads, hash_mb, options=None):
        eng = FakeEngine()
        created.append(eng)
        # 模拟 GUI 线程在「加载模型 10~30 秒」期间点了停止 / 关了窗口
        w_ref[0]._run = False
        return eng

    w_ref: list = []
    w = make_worker()
    w_ref.append(w)
    _patch(fake_create_engine)

    w.run()

    check("确实创建过引擎", len(created) == 1, f"创建 {len(created)} 次")
    check("新建的引擎被关闭（quit 恰好一次）",
          created and created[0].quit_called == 1,
          f"quit_called={created[0].quit_called if created else 'N/A'}")
    check("stdin 被显式关闭（避免 GC 时刷管道报错）",
          created and created[0].proc.stdin.closed)


# ---------------------------------------------------------------------------
# 用例 2：正常退出路径自己收引擎（不依赖 GUI 线程调 shutdown）
# ---------------------------------------------------------------------------
def case_dispose_on_exit() -> None:
    print()
    print("[2] run() 退出时自己收掉引擎（不再依赖 shutdown()）")
    created: list[FakeEngine] = []

    def fake_create_engine(threads, hash_mb, options=None):
        eng = FakeEngine()
        created.append(eng)
        return eng

    w_ref: list = []
    w = make_worker()

    class _StopAfterGrab(ScreenSource):
        """第一次抓帧就要求停止，让主循环干净退出。"""

        def grab(self):
            w_ref[0]._run = False
            return None

    w.source = _StopAfterGrab()
    w.source.hwnd = None
    w_ref.append(w)
    _patch(fake_create_engine)

    w.run()

    check("确实创建过引擎", len(created) == 1, f"创建 {len(created)} 次")
    check("退出路径上引擎被关闭（quit 恰好一次）",
          created and created[0].quit_called == 1,
          f"quit_called={created[0].quit_called if created else 'N/A'}")
    check("run() 结束后 self._engine 已置空", w._engine is None)


# ---------------------------------------------------------------------------
# 用例 3：运行日志追加而非覆盖 + 超限轮转
# ---------------------------------------------------------------------------
def case_log_append_and_rotate() -> None:
    print()
    print("[3] 运行日志：追加写入 + 超限轮转（保留上一次的运行轨迹）")
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        p = base / "XiangQiLens.log"

        # 模拟上一次运行留下的日志
        p.write_text("上一次运行的日志内容\n", encoding="utf-8")

        real_base = app_vision.HERE
        try:
            app_vision.HERE = base
            _, fh = app_vision.Worker._open_log()
            fh.write("本次运行的日志\n")
            fh.close()
        finally:
            app_vision.HERE = real_base
        text = p.read_text(encoding="utf-8")
        check("上一次的日志没被覆盖", "上一次运行的日志内容" in text, text[:40])
        check("本次日志已追加", "本次运行的日志" in text)

        # 超限轮转
        p.write_text("x" * (app_vision.Worker._LOG_MAX_BYTES + 10),
                     encoding="utf-8")
        try:
            app_vision.HERE = base
            _, fh = app_vision.Worker._open_log()
            fh.write("轮转后\n")
            fh.close()
        finally:
            app_vision.HERE = real_base
        check("超限后生成了 .log.1", (base / "XiangQiLens.log.1").is_file())
        check("轮转后主日志重新开始",
              not p.read_text(encoding="utf-8").startswith("x" * 100))


def main() -> int:
    print("引擎生命周期与日志测试（不需要窗口 / 不启动真实引擎）")
    try:
        case_stop_during_create()
        case_dispose_on_exit()
        case_log_append_and_rotate()
    except Exception as exc:                     # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"\n[失败] 测试自身异常: {exc}")
        return 1

    print()
    print(f"通过 {PASS} / 失败 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
