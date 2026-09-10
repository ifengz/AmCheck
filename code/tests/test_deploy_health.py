import subprocess
import textwrap
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"


def health_script(url):
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    script_start = next(i for i, line in enumerate(lines) if line.strip() == "script: |")
    block = []
    for line in lines[script_start + 1 :]:
        if line.startswith("            "):
            block.append(line[12:])
        elif line.strip():
            break
    health_start = block.index("for i in $(seq 1 10); do")
    script = textwrap.dedent("\n".join(block[health_start:]))
    return script.replace("http://127.0.0.1:8765/", url).replace(
        "$(seq 1 10)", "$(seq 1 2)"
    ).replace("sleep 2", "sleep 0.05")


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


if __name__ == "__main__":
    unittest.main()
