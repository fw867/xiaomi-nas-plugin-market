#!/usr/bin/env python3
"""SSH 开关插件：手动启停 dropbear，并可选开机自动保持运行。

背景：/lib/minas/boot_check.sh 的 ssh_check() 每次开机检查
sysmode=factory / channel=develop / RPMB 标志 ssh_en=true，
三者皆不满足时执行 `systemctl stop dropbear.socket` 关闭 SSH。
部分设备 RPMB 写入失效（mitee_tool rpmb set ssh_en true 报
"rpmb set verify failed"），该标志无法持久化。
本插件通过 cron 守护在开机后重新拉起 dropbear.socket，实现可开关的开机自启。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "18130"))
BASE_PATH = os.environ.get("BASE_PATH", "/")
WEB_DIR = Path(os.environ.get("WEB_DIR", Path(__file__).resolve().parent / "web"))
RUNTIME_DIR = Path(os.environ.get("RUNTIME_DIR", Path(__file__).resolve().parent))
STATE_FILE = Path(os.environ.get("STATE_FILE", "/data/plugin/ssh-control/state.json"))
DROPBEAR_UNIT = "dropbear.socket"
KEEPALIVE_SCRIPT = RUNTIME_DIR / "keepalive.sh"
CRON_TAG = "sshcontrol"
LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# systemd / cron 交互
# ---------------------------------------------------------------------------

def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def ssh_running() -> bool:
    return _run(["systemctl", "is-active", "--quiet", DROPBEAR_UNIT]).returncode == 0


def ssh_enabled() -> bool:
    return _run(["systemctl", "is-enabled", "--quiet", DROPBEAR_UNIT]).returncode == 0


def start_ssh() -> tuple[bool, str]:
    result = _run(["systemctl", "start", DROPBEAR_UNIT])
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "启动失败").strip()
    return True, ""


def stop_ssh() -> tuple[bool, str]:
    result = _run(["systemctl", "stop", DROPBEAR_UNIT])
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "停止失败").strip()
    return True, ""


def read_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(STATE_FILE)


def autostart_enabled() -> bool:
    return bool(read_state().get("autostart"))


def _cron_lines() -> list[str]:
    result = _run(["crontab", "-l"])
    if result.returncode != 0:
        return []
    return result.stdout.splitlines()


def _write_cron(lines: list[str]) -> bool:
    payload = "\n".join(line for line in lines if line.strip())
    result = subprocess.run(
        ["crontab", "-"],
        input=payload + "\n" if payload else "",
        capture_output=True, text=True, check=False,
    )
    return result.returncode == 0


def sync_cron(enabled: bool) -> tuple[bool, str]:
    """根据开关状态增删 cron 守护条目。

    用 `sh <脚本>` 调用而不是直接执行：发布包解压后文件权限统一为 0644，
    直接执行会因缺少可执行位失败。
    """
    lines = [line for line in _cron_lines() if CRON_TAG not in line]
    if enabled:
        lines.append(f"* * * * * sh {KEEPALIVE_SCRIPT} #@{CRON_TAG}")
    if not _write_cron(lines):
        return False, "写入 crontab 失败"
    return True, ""


def set_autostart(enabled: bool) -> tuple[bool, str]:
    ok, error = sync_cron(enabled)
    if not ok:
        return False, error
    state = read_state()
    state["autostart"] = enabled
    write_state(state)
    return True, ""


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "XiaomiSshControl/0.1"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} [{self.log_date_time_string()}] {fmt % args}")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: object) -> None:
        self._send(status, json_bytes(value), "application/json; charset=utf-8")

    def _status(self) -> dict:
        return {
            "ok": True,
            "sshRunning": ssh_running(),
            "sshEnabled": ssh_enabled(),
            "autostart": autostart_enabled(),
            "unit": DROPBEAR_UNIT,
        }

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        candidate = (WEB_DIR / relative).resolve()
        try:
            candidate.relative_to(WEB_DIR.resolve())
        except ValueError:
            self._json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "forbidden"})
            return
        if not candidate.is_file():
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
            return
        import mimetypes
        mime = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        cache = "no-store" if candidate.name == "index.html" else "public, max-age=300"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", cache)
        body = candidate.read_bytes()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/status":
            self._json(HTTPStatus.OK, self._status())
            return
        if path == "/healthz":
            self._json(HTTPStatus.OK, {"ok": True})
            return
        if path in ("/", "/index.html"):
            self._serve_static("index.html")
            return
        self._serve_static(path)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "无效请求长度"})
            return
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "请求体必须是 JSON"})
            return
        if not isinstance(body, dict):
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "请求体必须是对象"})
            return

        with LOCK:
            if path == "/api/action":
                action = str(body.get("action", ""))
                if action == "start":
                    ok, error = start_ssh()
                elif action == "stop":
                    # 手动停止同时关闭自启，避免守护把它拉回来
                    ok, error = stop_ssh()
                    if ok:
                        set_autostart(False)
                else:
                    self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"未知操作：{action}"})
                    return
                if not ok:
                    self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": error})
                    return
                self._json(HTTPStatus.OK, self._status())
                return

            if path == "/api/autostart":
                enabled = bool(body.get("enabled"))
                ok, error = set_autostart(enabled)
                if not ok:
                    self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": error})
                    return
                if enabled and not ssh_running():
                    start_ssh()
                self._json(HTTPStatus.OK, self._status())
                return

        self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})


def main() -> int:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Xiaomi SSH control listening on http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
