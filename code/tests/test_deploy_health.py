"""deploy.sh 健康检查回路测试。

服务器真实部署脚本是 code/deploy/deploy.sh(与 /www/wwwroot/amcheck_git/deploy.sh
同源)。这里抽取它的重试健康检查段,用本地 HTTP 桩验证:
- 200 → DEPLOY_OK 退出 0
- 404/500/连接拒绝 → 重试耗尽后 DEPLOY_HEALTH_FAIL 退出 1
测试时把重试次数与间隔缩短,避免拖慢用例。
"""

import re
import subprocess
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEPLOY_SH = Path(__file__).resolve().parents[1] / "deploy" / "deploy.sh"


def health_script(url):
    """抽取 deploy.sh 的健康检查段:从**含 curl 的那个** `for i in` 起到文件尾。

    注意 ①:脚本里有多个 `for i in` 循环(还有等 SIGTERM 生效的那个),
    不能像以前那样取第一个 —— 否则抽出来的是停止流程,测的不是健康检查。
    注意 ②:必须带上 done 之后的 `echo DEPLOY_HEALTH_FAIL; exit 1`——
    bash 的 if 条件不成立时整条 if 返回 0,循环本身不传播失败,
    失败退出码全靠循环后的显式 exit 1。
    注意 ③:**把 pkill 换成 true**。这段尾巴里有「健康检查失败就强杀」的逻辑,
    原样跑起来会去 kill 开发机上正在运行的 ui.py 实例。
    注意 ④:URL 里现在写的是 `${PORT}`,抽取段没有 PORT 定义,所以先补上再替换。
    """
    lines = DEPLOY_SH.read_text(encoding="utf-8").splitlines()
    curl_at = next(i for i, ln in enumerate(lines) if "curl -fs" in ln)
    start = max(i for i in range(curl_at)
                if lines[i].strip().startswith("for i in"))
    script = "\n".join(lines[start:])
    script = re.sub(r"for i in [0-9 ]+;", "for i in 1 2;", script)
    script = script.replace("sleep 2", "sleep 0.05")
    script = script.replace("pkill", "true")
    script = script.replace('"http://127.0.0.1:${PORT}/"', f'"{url}"')
    return "PORT=8765\nport_busy() { return 1; }\n" + script


def run_health(url):
    return subprocess.run(
        ["bash", "-c", health_script(url)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(self.server.response_status)
        self.end_headers()

    def log_message(self, *_args):
        pass


def run_server(status):
    server = ThreadingHTTPServer(("127.0.0.1", 0), StatusHandler)
    server.response_status = status
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class DeployHealthTests(unittest.TestCase):
    def test_http_200_succeeds(self):
        server, thread = run_server(200)
        try:
            result = run_health(f"http://127.0.0.1:{server.server_port}/")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DEPLOY_OK", result.stdout)

    def test_http_404_fails(self):
        server, thread = run_server(404)
        try:
            result = run_health(f"http://127.0.0.1:{server.server_port}/")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_http_500_fails(self):
        server, thread = run_server(500)
        try:
            result = run_health(f"http://127.0.0.1:{server.server_port}/")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_connection_refusal_after_retries_fails(self):
        server, thread = run_server(200)
        port = server.server_port
        server.shutdown()
        server.server_close()
        thread.join()

        result = run_health(f"http://127.0.0.1:{port}/")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("DEPLOY_HEALTH_FAIL", result.stderr)


class DeployScriptConventionTests(unittest.TestCase):
    """部署脚本里几条「别改回去」的约定(踩过坑或明确要求过)。

    这些都是**顺序敏感**的:环境变量/ulimit 必须出现在拉起进程之前,
    否则对子进程无效,但脚本照样「跑成功」——典型的静默失效。
    """

    @classmethod
    def setUpClass(cls):
        cls.lines = DEPLOY_SH.read_text(encoding="utf-8").splitlines()
        cls.launch = next(i for i, ln in enumerate(cls.lines)
                          if "nohup" in ln and "ui.py" in ln)

    def test_log_dir_is_under_wwwlogs(self):
        """日志落 /www/wwwlogs(宝塔日志目录),不再写 /tmp。"""
        body = "\n".join(self.lines)
        self.assertIn("/www/wwwlogs/amcheck", body)
        self.assertNotIn("/tmp/amcheck_ui.log", body)

    def test_python_is_unbuffered(self):
        """PYTHONUNBUFFERED=1 必须在拉起 ui.py 的那一行上。

        不加的话 stdout 走块缓冲,日志会延迟几十 KB 才落盘,
        排查「服务起了但没反应」时看不到任何东西。
        """
        self.assertIn("PYTHONUNBUFFERED=1", self.lines[self.launch])

    def test_fd_limit_raised_before_launch(self):
        """ulimit -n 65535 必须在拉起进程之前,子进程才继承得到。"""
        idx = next((i for i, ln in enumerate(self.lines)
                    if ln.strip().startswith("ulimit -n 65535")), None)
        self.assertIsNotNone(idx, "找不到 ulimit -n 65535")
        self.assertLess(idx, self.launch)

    def test_no_bare_file_test_and_chain(self):
        """禁止 `[ -f X ] && Y` —— set -e 下文件不存在会直接退出脚本。"""
        trap = re.compile(r"^\s*\[\s.*?\]\s*&&")
        offenders = [ln for ln in self.lines if trap.match(ln)]
        self.assertEqual(offenders, [], f"改用 if/then: {offenders}")

    def test_failure_dumps_log_tail(self):
        """健康检查失败时把日志尾巴打到 stderr,否则线上只能看到一句 FAIL。"""
        body = "\n".join(self.lines)
        self.assertIn("tail -n 50", body)


class DeployRestartHardeningTests(unittest.TestCase):
    """P0-2 重启防呆(2026-09-14 僵死事故的整改项)。

    事故:进程僵死时 SIGTERM 无效,而旧脚本 `pkill → sleep 2 → 启动`,
    新实例抢不到端口、健康检查失败,旧僵尸还挂在后台。
    """

    @classmethod
    def setUpClass(cls):
        cls.body = DEPLOY_SH.read_text(encoding="utf-8")
        cls.lines = cls.body.splitlines()

    def _index(self, needle):
        idx = [i for i, ln in enumerate(self.lines) if needle in ln]
        self.assertTrue(idx, f"deploy.sh 里找不到 {needle!r}")
        return idx

    def _launch(self):
        """真正拉起 ui.py 的那一行(跳过头部注释里举例的 nohup)。"""
        return next(i for i, ln in enumerate(self.lines)
                    if "nohup" in ln and "ui.py" in ln
                    and not ln.lstrip().startswith("#"))

    def test_kills_with_sigkill_before_restart(self):
        """TERM 无效时必须升级到 KILL,否则新实例永远起不来。"""
        term = self._index('pkill -f "$APP_PATTERN"')[0]
        kill = self._index('pkill -9 -f "$APP_PATTERN"')
        self.assertTrue(any(i > term for i in kill),
                        "缺少 SIGKILL 升级(或排在 TERM 之前,等于没升级)")

    def test_waits_for_port_release_before_launch(self):
        """启动前必须确认端口已释放。"""
        self.assertIn("port_busy()", self.body)
        busy_use = self._index("if ! port_busy; then")[0]
        self.assertLess(busy_use, self._launch(), "端口释放检查必须在启动之前")

    def test_port_comes_from_env(self):
        """端口走 AMREVIEW_PORT,别再写死 —— 改端口要和应用同步。"""
        self.assertIn("AMREVIEW_PORT", self.body)
        self.assertNotIn("http://127.0.0.1:8765/", self.body)

    def test_empty_pattern_is_refused(self):
        """APP_PATTERN 为空时拒绝执行:空模式下的 pkill 行为不可预期。"""
        self.assertIn('[ -z "$APP_PATTERN" ]', self.body)

    def test_failed_launch_is_cleaned_up(self):
        """健康检查失败要把半死不活的新实例收掉,别留给下一次部署撞端口。"""
        fail = self._index("DEPLOY_HEALTH_FAIL")[0]
        self.assertTrue(any(i > fail for i in self._index('pkill -9 -f "$APP_PATTERN"')),
                        "健康检查失败后没有清理新实例")


if __name__ == "__main__":
    unittest.main()
