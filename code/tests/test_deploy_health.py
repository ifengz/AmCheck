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
    """抽取 deploy.sh 的健康检查段:`for i in` 起到文件尾。

    注意必须带上 done 之后的 `echo DEPLOY_HEALTH_FAIL; exit 1`——
    bash 的 if 条件不成立时整条 if 返回 0,循环本身不传播失败,
    失败退出码全靠循环后的显式 exit 1。
    """
    lines = DEPLOY_SH.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines)
                 if ln.strip().startswith("for i in"))
    script = "\n".join(lines[start:])
    script = re.sub(r"for i in [0-9 ]+;", "for i in 1 2;", script)
    script = script.replace("sleep 2", "sleep 0.05")
    return script.replace("http://127.0.0.1:8765/", url)


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


if __name__ == "__main__":
    unittest.main()
