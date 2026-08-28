"""e2e 进程编排：配置→环境变量、进程启停、就绪探测、日志汇总。

- 四组件均以 ``uvicorn app.main:app --port <port>`` 独立启动；
- 组件配置经环境变量覆盖注入（YAML 扁平化 + 前缀，如 LEVEL1_SERVICE__PORT）；
- mock 组件以脚本方式启动（mock_provider / mock_site）。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
E2E_DIR = os.path.join(ROOT, "e2e")
CONFIG_DIR = os.path.join(E2E_DIR, "configs")
LOG_DIR = os.path.join(E2E_DIR, "logs")
VENV_PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")

COMPONENT_DIRS = {
    "level1": os.path.join(ROOT, "level1_pool"),
    "level2": os.path.join(ROOT, "level2_pool"),
    "proxy": os.path.join(ROOT, "proxy"),
}

# 端口约定
P_LEVEL1 = 8000
P_BAIDU = 8001
P_GONGSHANG = 8002
P_PROXY = 9000
P_MOCK_SITE = 9100
P_MOCK_PROVIDER = 20000
P_MOCK_LEVEL1 = 8100
P_MOCK_LEVEL1_TTL = 8110
P_TMP_EMPTY = 8101
P_TMP_MOCK_L2 = 8102
P_TMP_TTL = 8111
P_TMP_RESTART = 8120

# level2 组件读取的运行配置路径（app.main._CONFIG_PATH）
_L2_CFG_PATH = os.path.join(ROOT, "level2_pool", "config", "level2_pool.yaml")

BASE = {
    "level1": f"http://127.0.0.1:{P_LEVEL1}",
    "baidu": f"http://127.0.0.1:{P_BAIDU}",
    "gongshang": f"http://127.0.0.1:{P_GONGSHANG}",
    "proxy": f"http://127.0.0.1:{P_PROXY}",
    "mock_site": f"http://127.0.0.1:{P_MOCK_SITE}",
    "mock_provider": f"http://127.0.0.1:{P_MOCK_PROVIDER}",
    "mock_level1": f"http://127.0.0.1:{P_MOCK_LEVEL1}",
}


def ensure_log_dir() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# 配置 → 环境变量
# ---------------------------------------------------------------------------


def _flatten(node: dict, prefix: str, env: dict, path: str = "") -> None:
    for k, v in node.items():
        key = f"{path}__{k}" if path else k
        full = f"{prefix}{key}".upper()
        if isinstance(v, dict):
            _flatten(v, prefix, env, key)
        elif isinstance(v, list):
            continue  # 列表不参与 settings 环境变量覆盖
        elif v is not None:
            env[full] = str(v)


def config_to_env(yaml_path: str, prefix: str, overrides: dict | None = None) -> dict:
    """读取 YAML 配置并扁平化为 ``{PREFIX_...: value}`` 环境变量。"""
    with open(yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    env: dict[str, str] = {}
    _flatten(data, prefix, env)
    if overrides:
        env.update({str(k).upper(): str(v) for k, v in overrides.items()})
    return env


# ---------------------------------------------------------------------------
# HTTP / 端口工具
# ---------------------------------------------------------------------------


def http_get_json(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def port_in_use(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1.0)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


# ---------------------------------------------------------------------------
# 进程封装
# ---------------------------------------------------------------------------


class Proc:
    def __init__(self, name: str, cmd: list[str], cwd: str, env: dict, log_file: str):
        self.name = name
        self.cmd = cmd
        self.cwd = cwd
        self.env = env
        self.log_file = log_file
        self.proc: subprocess.Popen | None = None
        self._logf = None

    def start(self) -> None:
        ensure_log_dir()
        self._logf = open(self.log_file, "ab", buffering=0)
        self.proc = subprocess.Popen(
            self.cmd,
            cwd=self.cwd,
            env=self.env,
            stdout=self._logf,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return self

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self, timeout: float = 10.0) -> None:
        if self.proc is not None:
            if self.proc.poll() is None:
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=timeout)
                except (subprocess.TimeoutExpired, OSError):
                    try:
                        self.proc.kill()
                        self.proc.wait(timeout=5)
                    except (subprocess.TimeoutExpired, OSError):
                        pass
            self.proc = None
        if self._logf is not None:
            try:
                self._logf.write(b"\n=== process stopped ===\n")
                self._logf.flush()
                self._logf.close()
            except Exception:
                pass
            self._logf = None


# ---------------------------------------------------------------------------
# 服务编排
# ---------------------------------------------------------------------------


class Services:
    def __init__(self) -> None:
        ensure_log_dir()
        self.procs: dict[str, Proc] = {}

    def start_uvicorn(
        self,
        name: str,
        component: str,
        config: str,
        prefix: str,
        port: int,
        overrides: dict | None = None,
    ) -> Proc:
        if port_in_use(port):
            # Windows 下进程终止后端口可能短暂未释放（TIME_WAIT/关闭窗口），
            # 短暂重试避免误判（如 E2E-04 重启 level1）。
            deadline = time.time() + 6.0
            while port_in_use(port):
                if time.time() > deadline:
                    raise RuntimeError(
                        f"port {port} still in use after wait; is an old process running?"
                    )
                time.sleep(0.5)
        src = os.path.join(CONFIG_DIR, config)
        # level2 的 app.main 仅在 config/level2_pool.yaml 存在时才读取 YAML+env；
        # 不存在时直接返回默认配置（env 被忽略）。因此把运行配置落到该路径。
        if component == "level2":
            os.makedirs(os.path.dirname(_L2_CFG_PATH), exist_ok=True)
            shutil.copyfile(src, _L2_CFG_PATH)
        env = dict(os.environ)
        env.update(config_to_env(src, prefix, overrides))
        cmd = [
            VENV_PY, "-m", "uvicorn", "app.main:app",
            "--host", "0.0.0.0", "--port", str(port),
        ]
        proc = Proc(name, cmd, COMPONENT_DIRS[component], env, os.path.join(LOG_DIR, f"{name}.log"))
        proc.start()
        self.procs[name] = proc
        return proc

    def start_script(self, name: str, script: str) -> Proc:
        env = dict(os.environ)
        cmd = [VENV_PY, os.path.join(E2E_DIR, script)]
        proc = Proc(name, cmd, E2E_DIR, env, os.path.join(LOG_DIR, f"{name}.log"))
        proc.start()
        self.procs[name] = proc
        return proc

    def stop(self, name: str | None = None) -> None:
        if name is None:
            for n in list(self.procs):
                self.stop(n)
            return
        proc = self.procs.pop(name, None)
        if proc is not None:
            proc.stop()

    def stop_all(self) -> None:
        self.stop()

    def wait_http(
        self,
        url: str,
        predicate=None,
        timeout: float = 30.0,
        interval: float = 1.0,
        desc: str = "",
    ):
        """轮询 GET url 直至 predicate(data) 为真；返回最终 data。"""
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                data = http_get_json(url)
                if predicate is None or predicate(data):
                    return data
                last = data
            except Exception as exc:  # noqa: BLE001
                last = exc
            time.sleep(interval)
        raise TimeoutError(
            f"timeout waiting {desc or url} ({timeout}s): last={last!r}"
        )


# ---------------------------------------------------------------------------
# 常用就绪谓词
# ---------------------------------------------------------------------------


def l1_pool_gte(n: int):
    return lambda d: d.get("data", {}).get("pool_size", -1) >= n


def l2_pool_gte(n: int):
    return lambda d: d.get("data", {}).get("pool_stats", {}).get("total", -1) >= n


def l2_free_gte(n: int):
    return lambda d: d.get("data", {}).get("pool_stats", {}).get("free_total", -1) >= n


def svc_alive():
    return lambda d: True


# ---------------------------------------------------------------------------
# 真实链路启动
# ---------------------------------------------------------------------------


def start_real_chain(s: Services) -> None:
    """启动真实链路：level1 + level2×2 + proxy。就绪仅指 HTTP 可访问。"""
    s.start_uvicorn("level1", "level1", "level1.yaml", "LEVEL1_", P_LEVEL1)
    s.wait_http(f"{BASE['level1']}/api/v1/status", timeout=90, desc="level1 status")
    s.start_uvicorn("level2_baidu", "level2", "level2_site_a.yaml", "LEVEL2_", P_BAIDU)
    s.wait_http(f"{BASE['baidu']}/api/v1/status", timeout=60, desc="baidu status")
    s.start_uvicorn("level2_gongshang", "level2", "level2_site_b.yaml", "LEVEL2_", P_GONGSHANG)
    s.wait_http(f"{BASE['gongshang']}/api/v1/status", timeout=60, desc="gongshang status")
    s.start_uvicorn(
        "proxy", "proxy", "proxy_routes.yaml", "PROXY_", P_PROXY,
        overrides={"PROXY_REGISTRY__ROUTE_FILE": os.path.join(CONFIG_DIR, "proxy_routes.yaml")},
    )
    s.wait_http(f"{BASE['proxy']}/api/v1/health", timeout=60, desc="proxy health")


def start_mock_env(s: Services) -> None:
    """启动 mock 辅助进程：mock_site + mock_provider + mock level1。"""
    s.start_script("mock_site", "mock_site.py")
    s.wait_http(f"{BASE['mock_site']}/", timeout=30, desc="mock_site")
    s.start_script("mock_provider", "mock_provider.py")
    s.wait_http(f"{BASE['mock_provider']}/admin/state", timeout=60, desc="mock_provider admin")
    s.start_uvicorn("mock_level1", "level1", "level1_mock.yaml", "LEVEL1_", P_MOCK_LEVEL1)
    s.wait_http(f"{BASE['mock_level1']}/api/v1/status", timeout=60, desc="mock level1 status")