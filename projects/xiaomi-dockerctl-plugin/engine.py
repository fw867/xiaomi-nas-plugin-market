"""Docker 控制台后端引擎 — 小米智能存储社区插件。

移植自 lvmaple/minas_plugin 的 `xiaomi-nas-plugin-dockerctl`（上游 `src/files/httpd.py`），
保留其容器/镜像/Compose/存储卷/网络管理逻辑；这里把与平台相关的部分拆出来：

* Docker CLI 以子进程方式调用（设备上 CLI 不在 PATH 里，默认 `/data/docker/docker`）；
* 运行期数据（操作日志、拉取时间、Compose 项目）落在 `DATA_DIR` 下，不再写 `/tmp`
  和 `/data/backup`，卸载插件时数据随目录一起消失；
* 新增「运行配置」（`config.json`）：Docker CLI 路径与 Compose 目录可以在插件页里改，
  换设备/换 CLI 路径不用改 systemd。

安全边界：本插件本身**就是一个 Docker 控制台**，能操作宿主机上的全部容器与镜像，
等价于 Docker 级 root 权限。鉴权在 HTTP 层（仅回环 + 小米客户端证书，见 server.py），
不要在不可信网络里把端口暴露出去。
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path

# 开发环境（源码树里直接跑）的回退值。页面上显示的是插件包的真实版本，
# 由 installed_version() 从自身所在的发布目录名里取。
VERSION = "0.1.0"
DEFAULT_PORT = 18210
# 小米 NAS 上 Docker CLI 不在 PATH 里；按顺序探测，DOCKER_BIN 环境变量优先。
DEFAULT_DOCKER_BIN = "/data/docker/docker"
DOCKER_CANDIDATES = ("/data/docker/docker", "/usr/bin/docker", "/usr/local/bin/docker")
DEFAULT_DATA_DIR = "/data/plugin/dockerctl/data"
TIMEOUT = 12
MAX_BODY = 1_000_000


class Error(RuntimeError):
    """用户可见的操作错误；status 决定 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def installed_version() -> str:
    """插件包的真实版本号。

    商店安装器把包解压到 <releaseRoot>/releases/<版本>-<时间戳>-<pid>/ 下，
    current 是指向它的符号链接，所以本文件所在目录名里就带着版本。源码树里
    跑（开发、预览）时解析不出来，回退到 VERSION。
    """
    parts = Path(__file__).resolve().parent.name.split("-")
    if len(parts) > 2 and parts[-1].isdigit() and parts[-2].isdigit():
        return "-".join(parts[:-2])
    return VERSION


# ---------------------------------------------------------------------------
# 运行配置：Docker CLI 路径 / Compose 目录
# ---------------------------------------------------------------------------

def default_data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR") or DEFAULT_DATA_DIR)


def default_config(data_dir) -> dict:
    data_dir = Path(data_dir)
    return {
        "docker_bin": os.environ.get("DOCKER_BIN") or DEFAULT_DOCKER_BIN,
        "compose_dir": os.environ.get("COMPOSE_DIR") or str(data_dir / "compose"),
    }


def _config_path(data_dir) -> Path:
    return Path(data_dir) / "config.json"


def load_config(data_dir) -> dict:
    """读取 config.json；文件缺失或内容损坏时回退到默认值（不让服务起不来）。"""
    config = default_config(data_dir)
    path = _config_path(data_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return config
    try:
        saved = json.loads(raw)
    except ValueError:
        return config
    if not isinstance(saved, dict):
        return config
    for key in ("docker_bin", "compose_dir"):
        value = saved.get(key)
        if isinstance(value, str) and value.strip() and os.path.isabs(value.strip()):
            config[key] = value.strip()
    return config


def save_config(data_dir, config: dict) -> None:
    """原子写回配置。0600 + LF：配置里有本机路径，不写 CRLF 以免跨平台 diff。"""
    path = _config_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(
            {
                "docker_bin": str(config.get("docker_bin") or DEFAULT_DOCKER_BIN),
                "compose_dir": str(config.get("compose_dir") or ""),
            },
            handle,
            ensure_ascii=False,
            indent=2,
        )
        handle.write("\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _validate_path(value, label: str) -> str:
    if not isinstance(value, str):
        raise Error(f"{label}须为字符串")
    value = value.strip()
    if (not value or len(value) > 1024
            or any(char in value for char in ("\x00", "\n", "\r"))):
        raise Error(f"{label}不合法")
    if not os.path.isabs(value):
        raise Error(f"{label}须为绝对路径")
    return value


# ---------------------------------------------------------------------------
# Docker CLI 调用
# ---------------------------------------------------------------------------

def resolve_docker_bin(configured: str) -> str:
    """找一个可执行的 Docker CLI：配置值 → 设备常见路径 → PATH。"""
    for candidate in (configured, *DOCKER_CANDIDATES):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which(configured) if configured else None
    return found or shutil.which("docker") or configured or DEFAULT_DOCKER_BIN


class Docker:
    """对 Docker CLI 的薄封装；所有调用都有超时，失败统一转成 (1, out, err)。"""

    def __init__(self, configured: str = DEFAULT_DOCKER_BIN):
        self.configured = (configured or DEFAULT_DOCKER_BIN).strip()
        self.bin = resolve_docker_bin(self.configured)

    def run(self, args, timeout=TIMEOUT):
        try:
            result = subprocess.run(
                [self.bin, *args], capture_output=True, text=True,
                errors="replace", timeout=timeout)
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return 1, "", "timeout"
        except FileNotFoundError:
            return 1, "", f"docker binary not found: {self.bin}"
        except OSError as exc:
            return 1, "", str(exc)

    def run_input(self, args, data, timeout=30):
        """把 data 从 stdin 喂给命令（`docker login --password-stdin` 用）。"""
        try:
            result = subprocess.run(
                [self.bin, *args], input=data, capture_output=True, text=True,
                errors="replace", timeout=timeout)
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return 1, "", "timeout"
        except FileNotFoundError:
            return 1, "", f"docker binary not found: {self.bin}"
        except OSError as exc:
            return 1, "", str(exc)

    def stream(self, args, on_line, timeout=1800, env=None, binary=None, on_start=None):
        """流式执行（拉取镜像），把每行输出交给 on_line，用于滚动进度。

        on_start 拿到 Popen 句柄，调用方据此实现「取消拉取」（直接 kill 进程）。
        """
        command = [binary or self.bin, *args]
        try:
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", bufsize=1, env=env)
        except (OSError, ValueError) as exc:
            return 1, str(exc)
        if on_start is not None:
            try:
                on_start(process)
            except Exception:  # noqa: BLE001 - 记录句柄失败不能影响拉取本身
                pass
        deadline = time.time() + timeout
        try:
            for raw in process.stdout:
                if time.time() > deadline:
                    process.kill()
                    return 1, "timeout"
                for fragment in re.split(r"[\r\n]+", raw):
                    fragment = fragment.strip()
                    if fragment:
                        on_line(fragment)
            process.wait(timeout=30)
        except (OSError, ValueError) as exc:
            try:
                process.kill()
            except OSError:
                pass
            return 1, str(exc)
        return process.returncode, ""


# ---------------------------------------------------------------------------
# 校验与参数拼装（逻辑与上游一致，便于对照）
# ---------------------------------------------------------------------------

def safe_name(name) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}", name or ""))


def safe_id(value) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\-:@/]{0,255}", value or ""))


def safe_addr(value) -> bool:
    """子网/网关的宽松校验（IPv4/IPv6 CIDR）。"""
    return bool(re.fullmatch(r"[0-9A-Fa-f.:/]{3,64}", value or ""))


def safe_registry(host) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.\-]{0,253}(:\d{1,5})?", host or ""))


def _to_port(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.isdigit():
        number = int(value)
    else:
        return None
    return number if 1 <= number <= 65535 else None


def proxy_error(proxy) -> str:
    """校验一次性 HTTP(S) 代理地址，避免把任意字符串交给 skopeo。"""
    if (not isinstance(proxy, str) or len(proxy) > 2048
            or any(char.isspace() or ord(char) < 32 for char in proxy)):
        return "代理地址不合法"
    try:
        from urllib.parse import urlsplit
        parsed = urlsplit(proxy)
        port = parsed.port
    except ValueError:
        return "代理地址不合法"
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or not port or "@" in parsed.netloc
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        return "请输入不含账号密码的 HTTP(S) 代理地址，如 http://192.168.1.2:7890"
    return ""


def _image_ref(ref: str) -> str:
    return ref if ":" in ref.rsplit("/", 1)[-1] or "@" in ref else ref + ":latest"


def build_run_args(body) -> tuple[list[str] | None, str | None]:
    """把部署表单拼成 `docker run` 参数；返回 (args, error)。"""
    image = (body.get("image") or "").strip()
    if not image:
        return None, "镜像名不能为空"
    if not safe_id(image):
        return None, "镜像名不合法"

    name = (body.get("name") or "").strip()
    if name and not safe_name(name):
        return None, "容器名不合法"

    args = ["run", "-d"]
    if name:
        args += ["--name", name]

    # ports: [{"host": 8080, "container": 80, "protocol": "tcp"}]
    for item in body.get("ports") or []:
        raw_host, raw_container = item.get("host"), item.get("container")
        if raw_host is None and raw_container is None:
            continue
        host, container = _to_port(raw_host), _to_port(raw_container)
        if host is None or container is None:
            return None, "端口须为 1-65535 整数"
        protocol = "udp" if item.get("protocol") == "udp" else "tcp"
        args += ["-p", f"{host}:{container}/{protocol}"]

    # volumes: [{"host": "/path", "container": "/path", "readonly": false}]
    for item in body.get("volumes") or []:
        host, container = item.get("host"), item.get("container")
        if host and container:
            readonly = ":ro" if item.get("readonly") else ""
            args += ["-v", f"{host}:{container}{readonly}"]

    # env: [{"key": "FOO", "value": "bar"}]
    for item in body.get("env") or []:
        key = item.get("key")
        if key:
            value = item.get("value")
            args += ["-e", f"{key}={value if value is not None else ''}"]

    # restart policy
    restart = (body.get("restart") or "").strip()
    if restart in ("no", "on-failure", "always", "unless-stopped"):
        args += ["--restart", restart]

    # network
    network = (body.get("network") or "").strip()
    if network and safe_name(network):
        args += ["--net", network]

    # extra raw flags（上游行为：解析失败就忽略，不阻断部署）
    extra = (body.get("extra") or "").strip()
    if extra:
        try:
            args += shlex.split(extra)
        except ValueError:
            pass

    args.append(image)
    # command override
    command = (body.get("command") or "").strip()
    if command:
        try:
            args += shlex.split(command)
        except ValueError:
            args += command.split()
    return args, None


def parse_json_list(output: str) -> list:
    items = []
    for line in (output or "").splitlines():
        line = line.strip()
        if line:
            try:
                items.append(json.loads(line))
            except ValueError:
                pass
    return items


def list_registry_auths() -> list:
    """Docker 引擎已保存凭据的仓库名（绝不返回凭据本身）。"""
    config_path = os.path.join(os.path.expanduser("~"), ".docker", "config.json")
    names = set()
    try:
        with open(config_path, encoding="utf-8") as handle:
            config = json.load(handle)
        names.update((config.get("auths") or {}).keys())
        names.update((config.get("credHelpers") or {}).keys())
    except (OSError, ValueError, AttributeError):
        pass
    return sorted(names)


# ---------------------------------------------------------------------------
# 操作日志（环形缓冲 + 落盘，重启后仍在）
# ---------------------------------------------------------------------------

class OpsLog:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.items: list[dict] = []

    def log(self, kind, target, ok, detail="") -> None:
        entry = {
            "ts": time.time(),
            "kind": kind,
            "target": target,
            "ok": bool(ok),
            "detail": (detail or "").strip()[-500:],
        }
        with self.lock:
            self.items = (self.items + [entry])[-200:]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def load(self) -> None:
        try:
            with self.path.open(encoding="utf-8") as handle:
                lines = handle.readlines()[-200:]
        except OSError:
            return
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                self.items.append(entry)

    def tail(self, count: int) -> list:
        with self.lock:
            return self.items[-count:]

    def clear(self) -> None:
        with self.lock:
            self.items = []
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("", encoding="utf-8")
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 镜像拉取时间（Docker 的 Created 是构建时间，不是本地拉取时间）
# ---------------------------------------------------------------------------

class PullTimes:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.items: dict = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(self.items, handle, ensure_ascii=False)
        os.replace(temporary, self.path)

    def load(self, docker, recovered_from_ops=()) -> None:
        """读拉取时间；文件缺失时用操作日志里成功的拉取记录恢复一次。"""
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                self.items = {
                    key: value for key, value in saved.items()
                    if isinstance(key, str) and isinstance(value, dict)}
            return
        except (OSError, ValueError):
            pass

        recent = {}
        for entry in recovered_from_ops:
            if not isinstance(entry, dict):
                continue
            ref = entry.get("target", "")
            if entry.get("kind") == "拉取镜像" and entry.get("ok"):
                recent[_image_ref(ref)] = entry.get("ts", 0)
            elif entry.get("kind") == "删除镜像" and entry.get("ok"):
                recent.pop(_image_ref(ref), None)
        for ref, pulled_at in recent.items():
            code, out, _ = docker.run(
                ["image", "inspect", "--format", "{{.Id}}", ref], timeout=10)
            if code == 0 and out.strip():
                self.items[ref] = {"id": out.strip(), "at": pulled_at}
        if self.items:
            try:
                self._save()
            except OSError:
                pass

    def record(self, docker, image, tag_as, pulled_at) -> None:
        code, out, _ = docker.run(
            ["image", "inspect", "--format", "{{.Id}}", image], timeout=10)
        if code or not out.strip():
            return
        with self.lock:
            for ref in (image, tag_as):
                if ref:
                    self.items[_image_ref(ref)] = {"id": out.strip(), "at": pulled_at}
            try:
                self._save()
            except OSError:
                pass

    def forget(self, ref) -> None:
        with self.lock:
            if self.items.pop(_image_ref(ref), None) is not None:
                try:
                    self._save()
                except OSError:
                    pass

    def attach(self, items) -> list:
        with self.lock:
            saved = dict(self.items)
        for item in items:
            ref = (item.get("Repository") or "") + ":" + (item.get("Tag") or "")
            entry = saved.get(ref) or {}
            pulled_at = entry.get("at", 0) if entry.get("id") == item.get("ID") else 0
            item["PulledAt"] = pulled_at if isinstance(pulled_at, (int, float)) and pulled_at > 0 else 0
        return items


# ---------------------------------------------------------------------------
# 异步拉取任务（滚动输出帧 + 取消）
# ---------------------------------------------------------------------------

class PullTask:
    def __init__(self):
        self.lock = threading.Lock()
        self.task = None

    def snapshot(self):
        with self.lock:
            task = self.task
            if not task:
                return None
            snapshot = dict(task)
            snapshot.pop("proc", None)
            snapshot.pop("helper_container", None)
            snapshot["frames"] = list(task["frames"])
            return snapshot


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------

class Engine:
    def __init__(self, data_dir=None, *, dev=False, docker=None):
        self.data_dir = Path(data_dir or default_data_dir())
        # docker login 的凭据、操作日志、Compose 项目都要落在这里；先建好 0700，
        # 否则 HOME 指向不存在的目录时 `docker login` 写 .docker/config.json 会失败。
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.data_dir, 0o700)
        except OSError:
            pass
        self.dev = dev
        self.config = load_config(self.data_dir)
        self.docker = docker or Docker(self.config["docker_bin"])
        self.compose_dir = Path(self.config["compose_dir"])
        self.ops = OpsLog(self.data_dir / "ops.log")
        self.pull_times = PullTimes(self.data_dir / "pull-times.json")
        self.pull = PullTask()
        self.ops.load()
        self.pull_times.load(self.docker, self.ops.items)

    # ---- 配置 ----
    def settings(self) -> dict:
        return {
            "version": installed_version(),
            "docker_bin": self.docker.configured,
            "docker_bin_resolved": self.docker.bin,
            "docker_bin_found": os.path.isfile(self.docker.bin),
            "compose_dir": str(self.compose_dir),
            "data_dir": str(self.data_dir),
            "dev": self.dev,
        }

    def save_settings(self, body) -> dict:
        if not isinstance(body, dict):
            raise Error("配置格式无效")
        docker_bin = body.get("docker_bin")
        compose_dir = body.get("compose_dir")
        if docker_bin is not None:
            docker_bin = _validate_path(docker_bin, "Docker CLI 路径")
            if not os.path.isfile(docker_bin) or not os.access(docker_bin, os.X_OK):
                raise Error("Docker CLI 不存在或不可执行：" + docker_bin)
        if compose_dir is not None:
            compose_dir = _validate_path(compose_dir, "Compose 目录")
            try:
                Path(compose_dir).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise Error("Compose 目录不可创建：" + str(exc)) from exc
        config = load_config(self.data_dir)
        if docker_bin:
            config["docker_bin"] = docker_bin
        if compose_dir:
            config["compose_dir"] = compose_dir
        save_config(self.data_dir, config)
        self.config = config
        if docker_bin:
            self.docker = Docker(docker_bin)
        if compose_dir:
            self.compose_dir = Path(compose_dir)
        return self.settings()

    # ---- 基础 ----
    def health(self) -> dict:
        return {
            "ok": True,
            "service": "dockerctl",
            "version": installed_version(),
            "ts": int(time.time()),
        }

    def info(self) -> dict:
        code, out, _ = self.docker.run(["version", "--format", "{{json .}}"], timeout=8)
        version = {}
        if code == 0 and out.strip():
            try:
                version = json.loads(out)
            except ValueError:
                pass
        code2, out2, _ = self.docker.run(["info", "--format", "{{json .}}"], timeout=10)
        info = {}
        if code2 == 0 and out2.strip():
            try:
                info = json.loads(out2)
            except ValueError:
                pass
        return {"docker": version, "info": info, "docker_bin": self.docker.bin}

    def overview(self) -> dict:
        code, out, error = self.docker.run(
            ["ps", "-a", "--format", "{{.State}}"], timeout=12)
        states = {"running": 0, "exited": 0, "paused": 0, "created": 0,
                  "restarting": 0, "other": 0}
        total = 0
        if code == 0:
            for line in out.splitlines():
                state = line.strip().lower()
                if not state:
                    continue
                total += 1
                if state in states:
                    states[state] += 1
                else:
                    states["other"] += 1
        code2, out2, _ = self.docker.run(["images", "-q"], timeout=10)
        images = len([line for line in out2.splitlines() if line.strip()]) if code2 == 0 else 0
        code3, out3, _ = self.docker.run(["volume", "ls", "-q"], timeout=8)
        volumes = len([line for line in out3.splitlines() if line.strip()]) if code3 == 0 else 0
        code4, out4, _ = self.docker.run(["network", "ls", "-q"], timeout=8)
        networks = len([line for line in out4.splitlines() if line.strip()]) if code4 == 0 else 0
        return {
            "containers_total": total,
            "containers_running": states.get("running", 0),
            "images": images,
            "volumes": volumes,
            "networks": networks,
            "states": states,
            "error": error if code else "",
        }

    # ---- 容器 ----
    def containers(self, show_all: bool) -> dict:
        args = ["ps", "--no-trunc", "--format", "{{json .}}"]
        if show_all:
            args.insert(1, "-a")
        code, out, error = self.docker.run(args, timeout=15)
        return {"items": parse_json_list(out), "error": error if code else ""}

    def container_detail(self, container_id: str) -> dict:
        self._check_id(container_id)
        code, out, error = self.docker.run(["inspect", container_id], timeout=10)
        detail = {}
        if code == 0 and out.strip():
            try:
                detail = json.loads(out)[0] if out.strip().startswith("[") else json.loads(out)
            except (ValueError, IndexError):
                pass
        return {"detail": detail, "error": error if code else ""}

    def container_logs(self, container_id: str, tail) -> dict:
        self._check_id(container_id)
        if not str(tail).isdigit():
            tail = "200"
        code, out, error = self.docker.run(
            ["logs", "--tail", str(tail), container_id], timeout=20)
        return {"logs": out, "error": error if code else ""}

    def container_stats(self, container_id: str) -> dict:
        self._check_id(container_id)
        code, out, error = self.docker.run(
            ["stats", "--no-stream", "--format", "{{json .}}", container_id], timeout=12)
        stats = {}
        if code == 0 and out.strip():
            try:
                stats = json.loads(out.strip().splitlines()[0])
            except (ValueError, IndexError):
                pass
        return {"stats": stats, "error": error if code else ""}

    def container_action(self, container_id: str, action: str, body) -> dict:
        self._check_id(container_id)
        if action == "remove":
            args = ["rm"]
            if body.get("force"):
                args.append("-f")
            args.append(container_id)
        else:
            args = [action, container_id]
        code, out, error = self.docker.run(args, timeout=20)
        self.ops.log("容器操作:" + action, container_id, code == 0, error if code else "")
        return self._result(code, out, error)

    # ---- 镜像 ----
    def images(self) -> dict:
        code, out, error = self.docker.run(
            ["images", "--no-trunc", "--format", "{{json .}}"], timeout=15)
        return {"items": self.pull_times.attach(parse_json_list(out)),
                "error": error if code else ""}

    def image_detail(self, image: str) -> dict:
        self._check_id(image)
        code, out, error = self.docker.run(["inspect", image], timeout=10)
        detail = {}
        if code == 0 and out.strip():
            try:
                detail = json.loads(out)[0] if out.strip().startswith("[") else json.loads(out)
            except (ValueError, IndexError):
                pass
        return {"detail": detail, "error": error if code else ""}

    def remove_image(self, image: str, body) -> dict:
        self._check_id(image)
        args = ["rmi"] + (["-f"] if body.get("force") else []) + [image]
        code, out, error = self.docker.run(args, timeout=20)
        if code == 0:
            self.pull_times.forget(image)
        self.ops.log("删除镜像", image, code == 0, error if code else "")
        return self._result(code, out, error)

    def pull_status(self) -> dict:
        return {"task": self.pull.snapshot()}

    def start_pull(self, body) -> dict:
        image = (body.get("image") or "").strip()
        tag_as = (body.get("tag_as") or "").strip()
        proxy = body.get("proxy") or ""
        if not image or not safe_id(image):
            raise Error("invalid image name")
        if tag_as and not safe_id(tag_as):
            raise Error("invalid tag name")
        if proxy:
            error = proxy_error(proxy)
            if error:
                raise Error(error)
            skopeo_bin = shutil.which(os.environ.get("SKOPEO_BIN", "skopeo"))
            if not skopeo_bin:
                raise Error("单次代理拉取需要在 NAS 安装 skopeo（或设置 SKOPEO_BIN）")
        else:
            skopeo_bin = ""

        with self.pull.lock:
            task = self.pull.task
            running = bool(task and task["status"] == "running")
            if running:
                snapshot = dict(task)
                snapshot.pop("proc", None)
                snapshot.pop("helper_container", None)
                snapshot["frames"] = list(task["frames"])
                if task["image"] == image:
                    # 同镜像重复提交：幂等返回，前端直接打开进度
                    return {"ok": True, "already": True, "task": snapshot}
                raise Error(
                    "已有拉取任务进行中: %s（同一时间只能拉一个，可在进度条上取消）"
                    % task["image"], 409)
            new_task = {
                "id": f"{int(time.time() * 1000)}", "image": image, "tag_as": tag_as,
                "using_proxy": bool(proxy), "status": "running", "frames": [],
                "error": "", "output": "", "started": time.time(),
                "updated": time.time(), "ended": 0.0,
            }
            self.pull.task = new_task
        threading.Thread(
            target=self._run_pull,
            args=(image, tag_as, new_task["id"], proxy, skopeo_bin),
            daemon=True).start()
        return {"ok": True, "task": self.pull.snapshot()}

    def _stream_pull(self, args, task_id, timeout=1800, binary=None, env=None):
        """执行拉取命令，把输出滚动进任务帧；返回 (code, output, error)。"""
        collected = []

        def remember(process):
            with self.pull.lock:
                task = self.pull.task
                if task and task["id"] == task_id:
                    task["proc"] = process

        def on_line(fragment):
            collected.append(fragment)
            with self.pull.lock:
                task = self.pull.task
                if task and task["id"] == task_id:
                    task["frames"] = (task["frames"] + [fragment])[-12:]
                    task["updated"] = time.time()

        code, error = self.docker.stream(
            args, on_line, timeout=timeout, binary=binary, env=env,
            on_start=remember)
        with self.pull.lock:
            task = self.pull.task
            if task and task["id"] == task_id:
                task["proc"] = None
        return code, "\n".join(collected), error

    def _run_pull(self, image, tag_as, task_id, proxy, skopeo_bin):
        if proxy:
            env = os.environ.copy()
            env.update({
                "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy,
                "http_proxy": proxy, "https_proxy": proxy,
                "NO_PROXY": "", "no_proxy": "",
            })
            code, out, error = self._stream_pull(
                ["copy", "docker://" + image, "docker-daemon:" + image],
                task_id, binary=skopeo_bin, env=env)
        else:
            code, out, error = self._stream_pull(["pull", image], task_id)
        if code == 0 and tag_as and tag_as != image:
            code2, out2, error2 = self.docker.run(["tag", image, tag_as], timeout=10)
            out += "\n" + out2
            if code2:
                code, error = code2, error + error2
        if code != 0 and not error:
            # docker 的报错混在输出流末尾（如 "failed to register layer: ..."）
            error = "\n".join(
                [line for line in (out or "").splitlines() if line.strip()][-3:])
        with self.pull.lock:
            task = self.pull.task
            if task and task["id"] == task_id and task["status"] == "running":
                task["status"] = "success" if code == 0 else "error"
                task["error"] = error if code else ""
                task["output"] = out[-4000:]
                task["ended"] = time.time()
                active = True
            else:
                active = False
        if active:
            if code == 0:
                self.pull_times.record(self.docker, image, tag_as, time.time())
            self.ops.log("拉取镜像", image, code == 0, error if code else "")

    def cancel_pull(self) -> dict:
        with self.pull.lock:
            task = self.pull.task
            if not task or task["status"] != "running":
                return {"ok": False, "error": "没有进行中的拉取"}
            process = task.get("proc")
            task["status"] = "error"
            task["error"] = "已手动取消"
            task["ended"] = time.time()
            image = task["image"]
        if process:
            try:
                process.kill()
            except OSError:
                pass
        self.ops.log("取消拉取", image, True, "")
        return {"ok": True}

    # ---- 仓库登录 ----
    def registry_auths(self) -> dict:
        return {"registries": list_registry_auths()}

    def registry_login(self, body) -> dict:
        server = (body.get("server") or "").strip()
        user = (body.get("username") or "").strip()
        password = body.get("password") or ""
        if not server or not safe_registry(server):
            raise Error("invalid registry")
        if not user or "\n" in user or "\r" in user:
            raise Error("invalid username")
        if not password:
            raise Error("password required")
        code, out, error = self.docker.run_input(
            ["login", "-u", user, "--password-stdin", server],
            password + "\n", timeout=30)
        self.ops.log("仓库登录", server, code == 0, error if code else "")
        return self._result(code, out, error)

    def registry_logout(self, body) -> dict:
        server = (body.get("server") or "").strip()
        if not server or not safe_registry(server):
            raise Error("invalid registry")
        code, out, error = self.docker.run(["logout", server], timeout=15)
        self.ops.log("仓库退出", server, code == 0, error if code else "")
        return self._result(code, out, error)

    # ---- 部署 ----
    def deploy_run(self, body) -> dict:
        args, error = build_run_args(body)
        if error:
            raise Error(error)
        code, out, error = self.docker.run(args, timeout=60)
        self.ops.log(
            "部署容器",
            (body.get("name") or body.get("image") or "").strip(),
            code == 0, error if code else "")
        result = self._result(code, out, error)
        result["container_id"] = out.strip()
        return result

    def _ensure_compose_dir(self) -> Path:
        self.compose_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self.compose_dir

    def deploy_compose(self, body) -> dict:
        name = (body.get("name") or "").strip() or f"stack-{int(time.time())}"
        if not safe_name(name):
            raise Error("项目名不合法")
        yaml_text = body.get("yaml") or body.get("compose") or ""
        if not yaml_text.strip():
            raise Error("compose YAML 不能为空")

        project_dir = self._ensure_compose_dir() / name
        project_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        yaml_path = project_dir / "docker-compose.yml"
        # 纯文本：显式 LF；图标等二进制资源只用二进制读写，不做换行清理。
        with yaml_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(yaml_text)

        code, out, error = self.docker.run(
            ["compose", "-p", name, "-f", str(yaml_path), "up", "-d"], timeout=120)
        if code != 0:
            # 回退到独立的 docker-compose 二进制（老设备上不一定有 compose 插件）
            try:
                result = subprocess.run(
                    ["docker-compose", "-p", name, "-f", str(yaml_path), "up", "-d"],
                    capture_output=True, text=True, errors="replace", timeout=120)
                code, out, error = result.returncode, result.stdout, result.stderr
            except FileNotFoundError:
                pass  # 保留原始错误
            except OSError as exc:
                error = str(exc)

        self.ops.log("部署 Compose", name, code == 0, error if code else "")
        return {
            "ok": code == 0,
            "project": name,
            "path": str(yaml_path),
            "output": out,
            "error": error if code else "",
        }

    def list_compose(self) -> dict:
        if not self.compose_dir.is_dir():
            return {"items": []}
        items = []
        for entry in sorted(os.listdir(self.compose_dir)):
            yaml_path = self.compose_dir / entry / "docker-compose.yml"
            if not yaml_path.is_file():
                continue
            code, out, _ = self.docker.run(
                ["compose", "-p", entry, "ps", "--format", "{{json .}}"], timeout=10)
            services = parse_json_list(out) if code == 0 else []
            items.append({"name": entry, "path": str(yaml_path), "services": services})
        return {"items": items}

    def compose_action(self, project: str, action: str) -> dict:
        if not safe_name(project):
            raise Error("invalid project")
        project_dir = self.compose_dir / project
        yaml_path = project_dir / "docker-compose.yml"
        if not yaml_path.is_file():
            raise Error("project not found", 404)
        if action == "down":
            code, out, error = self.docker.run(
                ["compose", "-p", project, "-f", str(yaml_path), "down"], timeout=60)
        else:
            # 只有 down 成功才删项目目录
            code, out, error = self.docker.run(
                ["compose", "-p", project, "-f", str(yaml_path),
                 "down", "--remove-orphans"], timeout=60)
            if code == 0:
                shutil.rmtree(project_dir, ignore_errors=True)
        self.ops.log("Compose " + action, project, code == 0, error if code else "")
        return self._result(code, out, error)

    # ---- 存储卷 ----
    def volumes(self) -> dict:
        code, out, error = self.docker.run(
            ["volume", "ls", "--format", "{{json .}}"], timeout=10)
        return {"items": parse_json_list(out), "error": error if code else ""}

    def volume_detail(self, name: str) -> dict:
        self._check_name(name, "invalid volume")
        code, out, error = self.docker.run(["volume", "inspect", name], timeout=8)
        detail = {}
        if code == 0 and out.strip():
            try:
                detail = json.loads(out)[0]
            except (ValueError, IndexError):
                pass
        return {"detail": detail, "error": error if code else ""}

    def create_volume(self, body) -> dict:
        name = (body.get("name") or "").strip()
        self._check_name(name, "invalid volume name")
        code, out, error = self.docker.run(["volume", "create", name], timeout=8)
        self.ops.log("创建存储卷", name, code == 0, error if code else "")
        return self._result(code, out, error)

    def remove_volume(self, name: str) -> dict:
        self._check_name(name, "invalid volume")
        code, out, error = self.docker.run(["volume", "rm", name], timeout=8)
        self.ops.log("删除存储卷", name, code == 0, error if code else "")
        return self._result(code, out, error)

    # ---- 网络 ----
    def networks(self) -> dict:
        code, out, error = self.docker.run(
            ["network", "ls", "--format", "{{json .}}"], timeout=10)
        return {"items": parse_json_list(out), "error": error if code else ""}

    def network_detail(self, network: str) -> dict:
        self._check_id(network)
        code, out, error = self.docker.run(["network", "inspect", network], timeout=8)
        detail = {}
        if code == 0 and out.strip():
            try:
                detail = json.loads(out)[0]
            except (ValueError, IndexError):
                pass
        return {"detail": detail, "error": error if code else ""}

    def create_network(self, body) -> dict:
        name = (body.get("name") or "").strip()
        driver = (body.get("driver") or "bridge").strip()
        self._check_name(name, "invalid network name")
        if driver not in ("bridge", "host", "overlay", "macvlan", "none"):
            driver = "bridge"
        args = ["network", "create", "--driver", driver]
        subnet = (body.get("subnet") or "").strip()
        gateway = (body.get("gateway") or "").strip()
        parent = (body.get("parent") or "").strip()
        if subnet:
            if not safe_addr(subnet):
                raise Error("invalid subnet")
            args += ["--subnet", subnet]
        if gateway:
            if not safe_addr(gateway):
                raise Error("invalid gateway")
            args += ["--gateway", gateway]
        if parent:
            if not safe_name(parent):
                raise Error("invalid parent interface")
            args += ["-o", f"parent={parent}"]
        args.append(name)
        code, out, error = self.docker.run(args, timeout=10)
        self.ops.log("创建网络", name, code == 0, error if code else "")
        return self._result(code, out, error)

    def remove_network(self, network: str) -> dict:
        self._check_id(network)
        code, out, error = self.docker.run(["network", "rm", network], timeout=10)
        self.ops.log("删除网络", network, code == 0, error if code else "")
        return self._result(code, out, error)

    # ---- 日志 ----
    def logs(self, tail) -> dict:
        try:
            count = int(tail)
        except (TypeError, ValueError):
            count = 200
        count = max(1, min(count, 500))
        return {"items": self.ops.tail(count)}

    def clear_logs(self) -> dict:
        self.ops.clear()
        return {"ok": True}

    # ---- 内部 ----
    def _check_id(self, value: str) -> None:
        if not safe_id(value):
            raise Error("invalid id")

    def _check_name(self, value: str, message: str) -> None:
        if not value or not safe_name(value):
            raise Error(message)

    @staticmethod
    def _result(code, out, error) -> dict:
        return {"ok": code == 0, "output": out, "error": error if code else ""}
