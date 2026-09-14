"""统一日志与僵死取证。

事故背景(2026-09-14):服务进程活着但僵死 —— 请求无响应、监听队列从 33 涨到 51、
SIGTERM 无效。事后想复盘却发现**关键半小时的日志全丢了**,三件事叠加所致:

1. `deploy.sh` 用 `> /tmp/amcheck_ui.log` 重定向 —— 每次部署截断,且 /tmp 不持久;
2. Python 的 stdout 在非 tty(重定向到文件)下是**块缓冲**,日志要攒够几 KB 才落盘,
   被 SIGKILL 时缓冲区直接蒸发;
3. 业务代码用裸 `print`,没有时间戳、没有线程名、没有级别 —— 就算留下来了也难读。

本模块一次解决这三条,并提供「进程卡住了怎么看」的手段:

- ``install()``:统一 logging(时间戳 + 级别 + **线程名** + logger 名),同时写 stdout
  和 ``ui-YYYY-MM-DD.log``(按天切文件、自动清理过期文件);
- 强制行缓冲:``PYTHONUNBUFFERED=1`` 之外的兜底,本地直跑也生效;
- ``faulthandler``:
  - 致命信号(SIGSEGV/SIGABRT/SIGBUS…)直接把全线程栈写进 ``faulthandler.log``,
    否则现场只剩 shell 打的一句 "Segmentation fault";
  - **SIGUSR1 → ``kill -USR1 <pid>`` 立刻导出全线程栈到 ``threads.log``**。
    这是排查「进程活着但不动」最有效的一招:它走 faulthandler 的 **C 层** handler,
    不依赖 Python 主线程还能不能执行字节码 —— 主线程卡在阻塞的 C 调用(或 asyncio
    的 select)里时,``signal.signal()`` 注册的 Python handler 根本不会被调度。
    注意 ``faulthandler.register()`` 会**覆盖**掉默认行为且无法用 ``getsignal()``
    反查(那是 C 层注册的),要验证只能真的发一次信号。
    另外 dump 里只有线程**地址**没有名字,所以同一个信号还会触发一个 Python 层
    handler,把「线程名 → 地址」写进应用日志 —— 两边对得上才知道卡的是哪条线程。
  - 可选:``AMREVIEW_DUMP_TRACEBACK_SEC=N`` 每 N 秒自动 dump 一次,给无人值守的
    服务器用(默认关 —— 一直开着会把日志撑爆)。

目录选择顺序:``AMREVIEW_LOG_DIR`` → ``/www/wwwlogs/amcheck`` → 本模块同级的 ``logs/``。
前两个在本地开发机上通常不可写,所以**必须**有第三档回退,否则本地一跑就炸。
"""

from __future__ import annotations

import faulthandler
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

DEFAULT_LOG_DIR = "/www/wwwlogs/amcheck"
FALLBACK_LOG_DIR = Path(__file__).resolve().parent / "logs"
KEEP_DAYS = 14
DEFAULT_LEVEL = "INFO"

LOG_FORMAT = "%(asctime)s %(levelname)-5s [%(threadName)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_state: dict = {"installed": False, "dir": None, "keep": None}


def _writable(path: Path) -> bool:
    """真去写一下 —— 目录存在不等于当前用户有权限(os.access 对 root/容器会骗人)。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def pick_log_dir() -> Path:
    """按优先级挑一个**确实可写**的日志目录。"""
    candidates = [os.environ.get("AMREVIEW_LOG_DIR"), DEFAULT_LOG_DIR,
                  str(FALLBACK_LOG_DIR)]
    for cand in candidates:
        if not cand:
            continue
        p = Path(cand)
        if _writable(p):
            return p
    return FALLBACK_LOG_DIR          # 全都不行也得返回一个,后续写入失败会被吞


def prune(log_dir: Path, keep_days: int = KEEP_DAYS) -> list[str]:
    """删掉超过 keep_days 天的 ``ui-*.log``,返回被删的文件名。"""
    removed: list[str] = []
    cutoff = time.time() - keep_days * 86400
    try:
        for f in sorted(log_dir.glob("ui-*.log")):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed.append(f.name)
            except OSError:
                pass
    except OSError:
        pass
    return removed


class DailyFileHandler(logging.Handler):
    """按天切文件的 handler:写 ``ui-YYYY-MM-DD.log``,跨天自动换文件。

    用「按天一个新文件」而不是 logrotate,是因为服务器上未必配了轮转,
    而**没有轮转的日志迟早会把磁盘写满** —— 那就从"查不到"变成"起不来"了。
    每条都 flush,配合行缓冲,保证 SIGKILL 前写进去的就是能看到的。
    """

    def __init__(self, log_dir: Path, keep_days: int = KEEP_DAYS):
        super().__init__()
        self.log_dir = log_dir
        self.keep_days = keep_days
        self._day = ""
        self._fh = None

    def _open(self):
        day = datetime.now().strftime("%Y-%m-%d")
        return open(self.log_dir / f"ui-{day}.log", "a", encoding="utf-8",
                    buffering=1)

    def _stream(self):
        day = datetime.now().strftime("%Y-%m-%d")
        if day != self._day or self._fh is None:
            if self._fh is not None:
                try:
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None
            try:
                self._fh = self._open()
            except OSError:
                # 目录被人删了(或挂载掉了)也不能把日志变成异常源:重建一次再试
                self.log_dir.mkdir(parents=True, exist_ok=True)
                self._fh = self._open()
            self._day = day
            if day != getattr(self, "_last_pruned_day", None):
                prune(self.log_dir, self.keep_days)
                self._last_pruned_day = day
        return self._fh

    def emit(self, record: logging.LogRecord) -> None:
        try:
            fh = self._stream()
            fh.write(self.format(record) + "\n")
            fh.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        try:
            if self._fh is not None:
                self._fh.close()
        except Exception:
            pass
        self._fh = None
        super().close()


class FlushStreamHandler(logging.StreamHandler):
    """每条都 flush 的 stdout handler。

    logging.StreamHandler 默认只在 ``emit`` 里 write,不 flush ——
    stdout 被重定向到文件时是块缓冲,日志会滞后几 KB 才落盘。
    """

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        try:
            self.flush()
        except Exception:
            pass


def _open_side_file(log_dir: Path, name: str):
    """打开 faulthandler 的落点文件,并**保持引用**。

    faulthandler 拿的是文件对象;对象被 GC 掉后写入会静默失败,
    所以句柄必须挂在模块级 ``_state`` 上活到进程结束。
    """
    fh = open(log_dir / name, "a", encoding="utf-8", buffering=1)
    _state.setdefault("handles", []).append(fh)
    return fh


def _thread_name_map(signum, frame) -> None:
    """SIGUSR1 的 Python 层搭档:把「线程名 → 线程地址」写进应用日志。

    faulthandler 的 dump 只有地址(``Thread 0x16e297000``),没有名字 ——
    而排查时真正想知道的是「卡住的是 review-tracker 还是事件循环」。
    ``threading.get_ident()`` 返回的就是同一个原生线程 id,所以两边能对上:
    先在应用日志里找到名字对应的地址,再去 threads.log 里搜那个地址的栈。
    """
    log = logging.getLogger("amreview")
    for t in threading.enumerate():
        log.info("线程快照 %s → 0x%x alive=%s", t.name, t.ident or 0, t.is_alive())


def install_faulthandler(log_dir: Path, dump_interval: int = 0) -> None:
    """注册致命信号 dump + SIGUSR1 全线程栈导出。任何一步失败都不影响启动。"""
    try:
        fatal = _open_side_file(log_dir, "faulthandler.log")
        faulthandler.enable(file=fatal, all_threads=True)
    except Exception:
        try:
            faulthandler.enable()          # 退回 stderr
        except Exception:
            pass

    # 顺序要紧:Python 层 handler 必须先装,faulthandler 再用 chain=True 挂上去。
    # 反过来的话 signal.signal() 会把 faulthandler 的 C handler 覆盖掉,dump 就没了。
    try:
        signal.signal(signal.SIGUSR1, _thread_name_map)
        chained = True
    except (ValueError, OSError):
        chained = False                    # 非主线程装不了,退化成"只有栈、没有名字"

    try:
        threads = _open_side_file(log_dir, "threads.log")
        faulthandler.register(signal.SIGUSR1, file=threads, all_threads=True,
                              chain=chained)
        _state["threads_file"] = str(log_dir / "threads.log")
    except Exception:
        threads = None

    if dump_interval > 0 and threads is not None:
        try:
            faulthandler.dump_traceback_later(dump_interval, repeat=True,
                                              file=threads, exit=False)
            _state["dump_interval"] = dump_interval
        except Exception:
            pass


def install(level: str | None = None, name: str = "amreview") -> Path:
    """装好日志。幂等,返回实际使用的日志目录。"""
    if _state["installed"]:
        return _state["dir"]

    log_dir = pick_log_dir()
    keep = int(os.environ.get("AMREVIEW_LOG_KEEP_DAYS", KEEP_DAYS) or KEEP_DAYS)

    # 非 tty 下 stdout 默认块缓冲:显式切行缓冲,本地直跑(不经 deploy.sh)也生效
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except Exception:
            pass

    root = logging.getLogger()
    root.setLevel((level or os.environ.get("AMREVIEW_LOG_LEVEL", DEFAULT_LEVEL)).upper())
    for h in list(root.handlers):
        root.removeHandler(h)              # 别让 uvicorn/nicegui 的默认 handler 重复输出
    fmt = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    sh = FlushStreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    fh = DailyFileHandler(log_dir, keep)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    try:
        dump_interval = int(os.environ.get("AMREVIEW_DUMP_TRACEBACK_SEC", "0") or 0)
    except ValueError:
        dump_interval = 0
    install_faulthandler(log_dir, dump_interval)

    _state.update(installed=True, dir=log_dir, keep=keep)

    log = logging.getLogger(name)
    log.info("日志就绪 dir=%s 每天一个文件 保留 %d 天", log_dir, keep)
    if _state.get("threads_file"):
        log.info("进程卡住时:kill -USR1 %d → %s", os.getpid(),
                 _state["threads_file"])
    if dump_interval > 0:
        log.info("已开启每 %d 秒自动导出全线程栈", dump_interval)
    return log_dir


def log_dir() -> Path | None:
    """当前日志目录(install 之前为 None)。"""
    return _state["dir"]


def threads_log() -> Path | None:
    """SIGUSR1 导出的线程栈落点。"""
    f = _state.get("threads_file")
    return Path(f) if f else None


def get_logger(name: str) -> logging.Logger:
    """业务模块统一从这里拿 logger(没 install 也能正常输出到 stderr)。"""
    return logging.getLogger(name)
