"""logsetup 的回归测试(2026-09-14 僵死事故的整改项 P0-1)。

事故里最关键的一条教训是「日志没了」,所以这里盯四件事:
1. 日志目录选得对(env 优先、不可写要回退,别让本地一跑就炸);
2. 文件按天命名、内容带时间戳/级别/**线程名**;
3. 过期文件会被清理(没有轮转的日志迟早把磁盘写满);
4. ``kill -USR1`` 真能导出**全部**线程的栈 —— 这条必须在子进程里验,
   因为 faulthandler.register() 不可逆、且会覆盖信号默认行为,
   在测试进程里注册完就没法干净地还原。
"""

import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_DIR))

import logsetup  # noqa: E402


def _record(msg="hello %s", args=("world",), thread="probe-thread"):
    rec = logging.LogRecord("probe", logging.INFO, __file__, 1, msg, args, None)
    rec.threadName = thread          # 钉死线程名,断言才稳定
    return rec


class LogDirTests(unittest.TestCase):
    def test_env_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("AMREVIEW_LOG_DIR")
            os.environ["AMREVIEW_LOG_DIR"] = tmp
            try:
                self.assertEqual(logsetup.pick_log_dir(), Path(tmp))
            finally:
                if old is None:
                    os.environ.pop("AMREVIEW_LOG_DIR", None)
                else:
                    os.environ["AMREVIEW_LOG_DIR"] = old

    def test_unwritable_candidate_is_skipped(self):
        """把目录设成一个**已存在的文件**:mkdir 必然失败,得往后回退。

        这条防的是「本地没有 /www/wwwlogs,一跑就炸」——必须有兜底目录。
        """
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "not-a-dir"
            blocker.write_text("x")
            old = os.environ.get("AMREVIEW_LOG_DIR")
            os.environ["AMREVIEW_LOG_DIR"] = str(blocker)
            try:
                got = logsetup.pick_log_dir()
            finally:
                if old is None:
                    os.environ.pop("AMREVIEW_LOG_DIR", None)
                else:
                    os.environ["AMREVIEW_LOG_DIR"] = old
            self.assertNotEqual(got, blocker)
            self.assertTrue(got.is_dir(), f"回退目录必须真的可写:{got}")


class DailyFileHandlerTests(unittest.TestCase):
    def _handler(self, tmp):
        h = logsetup.DailyFileHandler(Path(tmp))
        h.setFormatter(logging.Formatter(logsetup.LOG_FORMAT, logsetup.DATE_FORMAT))
        return h

    def test_file_is_named_by_day_and_line_is_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = self._handler(tmp)
            try:
                h.emit(_record())
            finally:
                h.close()
            today = datetime.now().strftime("%Y-%m-%d")
            path = Path(tmp) / f"ui-{today}.log"
            self.assertTrue(path.exists(), f"没有按天建文件:{list(Path(tmp).iterdir())}")
            line = path.read_text(encoding="utf-8")
            # 时间戳 —— 事故里裸 print 的最大问题就是没有时间线
            self.assertRegex(line, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
            self.assertIn("INFO", line)
            # 线程名 —— 僵死时要能分辨"卡在哪个线程"
            self.assertIn("[probe-thread]", line)
            self.assertIn("hello world", line)

    def test_each_emit_is_flushed(self):
        """每条都 flush:被 SIGKILL 时缓冲区里的日志会整段蒸发。"""
        with tempfile.TemporaryDirectory() as tmp:
            h = self._handler(tmp)
            try:
                h.emit(_record(msg="first", args=()))
                today = datetime.now().strftime("%Y-%m-%d")
                text = (Path(tmp) / f"ui-{today}.log").read_text(encoding="utf-8")
                self.assertIn("first", text)   # 没 close 就能读到 = 已落盘
            finally:
                h.close()


class PruneTests(unittest.TestCase):
    def test_only_old_ui_logs_are_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            old = d / "ui-2020-01-01.log"
            fresh = d / "ui-2999-01-01.log"
            other = d / "console.log"
            for f in (old, fresh, other):
                f.write_text("x")
            stale = time.time() - 40 * 86400
            os.utime(old, (stale, stale))

            removed = logsetup.prune(d, keep_days=14)

            self.assertEqual(removed, [old.name])
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(other.exists(), "不该动非 ui-*.log 的文件")

    def test_missing_dir_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(logsetup.prune(Path(tmp) / "nope"), [])


class InstallTests(unittest.TestCase):
    """install() 会改全局 logging 状态,测完必须还原 ——
    否则后面 exec ui.py 的用例会往一个已经删掉的临时目录里写日志。"""

    def setUp(self):
        self._root = logging.getLogger()
        self._handlers = list(self._root.handlers)
        self._state = dict(logsetup._state)

    def tearDown(self):
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
        for h in self._handlers:
            root.addHandler(h)
        logsetup._state.clear()
        logsetup._state.update(self._state)

    def test_install_writes_daily_file_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("AMREVIEW_LOG_DIR")
            os.environ["AMREVIEW_LOG_DIR"] = tmp
            logsetup._state.clear()
            logsetup._state.update(installed=False, dir=None, keep=None)
            try:
                first = logsetup.install()
                logging.getLogger("probe").info("安装后写入的一行")
                again = logsetup.install()
            finally:
                if old is None:
                    os.environ.pop("AMREVIEW_LOG_DIR", None)
                else:
                    os.environ["AMREVIEW_LOG_DIR"] = old

            self.assertEqual(first, Path(tmp), "env 指定的目录没被采用")
            self.assertEqual(first, again, "install 必须幂等")
            today = datetime.now().strftime("%Y-%m-%d")
            text = (Path(tmp) / f"ui-{today}.log").read_text(encoding="utf-8")
            self.assertIn("安装后写入的一行", text)
            self.assertIn("日志就绪", text)
            # 线程名要在:install 在主线程里跑
            self.assertRegex(text, r"\[MainThread\]")


class InstallSafetyTests(unittest.TestCase):
    """``install()`` 是在 ``ui.py`` 的**导入期**跑的 —— 它抛异常等于服务起不来。

    而它存在的意义偏偏是「出事之后还能查到东西」,所以这里盯的不是日志写得好不好
    看,而是「坏配置 / 坏环境绝不能把进程带走」。这几条都是 push 前实测出来的:
    原来 ``int(os.environ[...])`` 和 ``setLevel(...)`` 都会对填错的变量抛
    ValueError,而它们跑在模块级。
    """

    ENV_KEYS = ("AMREVIEW_LOG_DIR", "AMREVIEW_LOG_KEEP_DAYS",
                "AMREVIEW_LOG_LEVEL", "AMREVIEW_DUMP_TRACEBACK_SEC")

    def setUp(self):
        self._root = logging.getLogger()
        self._handlers = list(self._root.handlers)
        self._level = self._root.level
        self._state = dict(logsetup._state)
        self._env = {k: os.environ.get(k) for k in self.ENV_KEYS}
        logsetup._state.clear()
        logsetup._state.update(installed=False, dir=None, keep=None)

    def tearDown(self):
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
        for h in self._handlers:
            root.addHandler(h)
        root.setLevel(self._level)
        logsetup._state.clear()
        logsetup._state.update(self._state)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_garbage_numeric_env_does_not_raise(self):
        """``AMREVIEW_LOG_KEEP_DAYS=abc`` 会让 int() 抛 ValueError 直接炸启动。"""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["AMREVIEW_LOG_DIR"] = tmp
            os.environ["AMREVIEW_LOG_KEEP_DAYS"] = "abc"
            os.environ["AMREVIEW_DUMP_TRACEBACK_SEC"] = "soon"
            d = logsetup.install()                      # 不许抛
            self.assertEqual(d, Path(tmp))
            self.assertEqual(logsetup._state["keep"], logsetup.KEEP_DAYS,
                             "填错要退回默认保留天数")

    def test_unknown_level_name_falls_back_to_info(self):
        """``AMREVIEW_LOG_LEVEL=verbose`` 会让 logging.setLevel() 抛 ValueError。"""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["AMREVIEW_LOG_DIR"] = tmp
            os.environ["AMREVIEW_LOG_LEVEL"] = "verbose"
            logsetup.install()
            self.assertEqual(logging.getLogger().level, logging.INFO)

    def test_keep_days_is_clamped_to_at_least_one(self):
        """填 0 会让 prune 把当天刚写下的日志当场删光 —— 越修越查不到。"""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["AMREVIEW_LOG_DIR"] = tmp
            os.environ["AMREVIEW_LOG_KEEP_DAYS"] = "0"
            logsetup.install()
            self.assertGreaterEqual(logsetup._state["keep"], 1)
            logging.getLogger("probe").info("别删我")
            today = datetime.now().strftime("%Y-%m-%d")
            self.assertTrue((Path(tmp) / f"ui-{today}.log").exists())

    def test_install_swallows_internal_failure(self):
        """兜底本身也要有兜底:挑目录都炸了,进程必须照常跑。"""
        def boom():
            raise RuntimeError("boom")

        original = logsetup.pick_log_dir
        logsetup.pick_log_dir = boom
        try:
            d = logsetup.install()
        finally:
            logsetup.pick_log_dir = original
        self.assertEqual(d, logsetup.FALLBACK_LOG_DIR)
        self.assertTrue(logsetup._state["installed"], "失败也要标成已装,避免反复重试")


class Sigusr1DumpTests(unittest.TestCase):
    """``kill -USR1 <pid>`` 必须导出**全部**线程栈(而不只是当前线程)。

    进程僵死时这是唯一还能用的取证手段:主线程可能卡在阻塞的 C 调用里,
    Python 层注册的 signal handler 根本不会被调度,只有 faulthandler 的
    C 层 handler 还能干活。所以在子进程里真发一次信号来验。
    """

    PROBE = """
import os, signal, sys, threading, time
sys.path.insert(0, {code_dir!r})
os.environ["AMREVIEW_LOG_DIR"] = {tmp!r}
import logsetup
d = logsetup.install()

def work():
    time.sleep(10)

threading.Thread(target=work, name="probe-worker", daemon=True).start()
time.sleep(0.3)
os.kill(os.getpid(), signal.SIGUSR1)
time.sleep(0.3)
print("LOGDIR", d)
"""

    def test_usr1_dumps_every_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = self.PROBE.format(code_dir=str(CODE_DIR), tmp=tmp)
            proc = subprocess.run([sys.executable, "-c", script],
                                  capture_output=True, text=True, timeout=30,
                                  cwd=str(CODE_DIR))
            self.assertEqual(proc.returncode, 0, proc.stderr)

            path = Path(tmp) / "threads.log"
            self.assertTrue(path.exists(), f"没有生成 threads.log:{proc.stdout}")
            dump = path.read_text(encoding="utf-8")
            self.assertIn("Current thread 0x", dump, "当前线程栈缺失")
            self.assertIn("Thread 0x", dump,
                          "只 dump 了当前线程 —— all_threads 没生效,"
                          "僵死时看不到卡住的那条线程")

            # dump 里只有地址没有名字:名字要能对回去,否则照样不知道卡的是哪条线程
            today = datetime.now().strftime("%Y-%m-%d")
            app_log = (Path(tmp) / f"ui-{today}.log").read_text(encoding="utf-8")
            self.assertIn("线程快照 MainThread", app_log)
            m = re.search(r"线程快照 probe-worker → (0x[0-9a-f]+)", app_log)
            self.assertIsNotNone(m, f"应用日志里没有线程名快照:\n{app_log}")
            addrs = {int(a, 16) for a in re.findall(r"Thread (0x[0-9a-f]+)", dump)}
            self.assertIn(int(m.group(1), 16), addrs,
                          "线程名对应的地址没出现在栈 dump 里,两边对不上")

    def test_install_logs_the_usr1_hint(self):
        """日志里要直接告诉运维怎么抓栈,不然没人知道有这功能。"""
        with tempfile.TemporaryDirectory() as tmp:
            script = self.PROBE.format(code_dir=str(CODE_DIR), tmp=tmp)
            subprocess.run([sys.executable, "-c", script],
                           capture_output=True, text=True, timeout=30,
                           cwd=str(CODE_DIR))
            today = datetime.now().strftime("%Y-%m-%d")
            text = (Path(tmp) / f"ui-{today}.log").read_text(encoding="utf-8")
            self.assertIn("kill -USR1", text)
            self.assertRegex(text, re.compile(r"kill -USR1 \d+"))


if __name__ == "__main__":
    unittest.main()
