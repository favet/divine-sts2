"""Synchronous Python client and persistent isolated-worker pool for NativeSim."""

from __future__ import annotations

import json
import copy
import os
import queue
import subprocess
import threading
import time
import uuid
from collections import deque, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterable

from .paths import REPOSITORY_ROOT, find_game_assembly, find_godot


_windows_spawn_lock = threading.Lock()
_SEM_FAILCRITICALERRORS = 0x0001
_SEM_NOGPFAULTERRORBOX = 0x0002
_SEM_NOOPENFILEERRORBOX = 0x8000


def _spawn_without_windows_error_dialogs(command: list[str], **options: Any) -> subprocess.Popen[str]:
    """Spawn one child that inherits suppressed Windows fault/open-file dialogs."""
    if os.name != "nt":
        return subprocess.Popen(command, **options)
    import ctypes

    set_error_mode = ctypes.windll.kernel32.SetErrorMode
    set_error_mode.argtypes = [ctypes.c_uint]
    set_error_mode.restype = ctypes.c_uint
    mode = _SEM_FAILCRITICALERRORS | _SEM_NOGPFAULTERRORBOX | _SEM_NOOPENFILEERRORBOX
    # Error mode is process-wide, so serialize the brief inherit-at-create window.
    with _windows_spawn_lock:
        previous = set_error_mode(mode)
        try:
            return subprocess.Popen(command, **options)
        finally:
            set_error_mode(previous)


class NativeSimError(RuntimeError):
    def __init__(self, code: str, message: str, details: Any = None):
        super().__init__(f"{code}: {message}")
        self.code, self.details = code, details


def _defaults() -> tuple[Path, Path, Path]:
    project = REPOSITORY_ROOT / "src" / "Sts2.NativeSim.GodotHost"
    if not project.exists():
        project = REPOSITORY_ROOT / "divine-sts2" / "src" / "Sts2.NativeSim.GodotHost"
    if not project.exists():
        project = REPOSITORY_ROOT / "native_sim" / "src" / "Sts2.NativeSim.GodotHost"
    if not project.exists():
        project = REPOSITORY_ROOT.parent / "divine-sts2" / "src" / "Sts2.NativeSim.GodotHost"
    if not project.exists():
        project = REPOSITORY_ROOT.parent / "native_sim" / "src" / "Sts2.NativeSim.GodotHost"
    return find_godot(), project, find_game_assembly()


class NativeWorker:
    """One Godot process and therefore one isolated set of native singletons."""

    def __init__(
        self,
        godot: str | Path | None = None,
        project: str | Path | None = None,
        assembly: str | Path | None = None,
        request_timeout: float | None = None,
    ):
        default_godot, default_project, default_assembly = _defaults()
        self.command = [str(godot or default_godot), "--headless", "--path", str(project or default_project), "--", "--server", str(assembly or default_assembly)]
        self._lock = threading.Lock()
        self._logs: deque[str] = deque(maxlen=100)
        self._stdout_lines: queue.Queue[str | None] = queue.Queue()
        self.request_timeout = request_timeout or float(os.environ.get("DIVINE_STS2_REQUEST_TIMEOUT", "60"))
        if self.request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        self._reset_state: dict[str, Any] | None = None
        self._reset_request: dict[str, Any] | None = None
        self._history: list[str] = []
        self._handle_histories: OrderedDict[str, list[str]] = OrderedDict()
        self.process: subprocess.Popen[str]
        self._start()
        try:
            self.build = self.hello()["game_build"]
        except Exception as startup_error:
            try:
                self.close()
            except Exception as cleanup_error:
                if isinstance(startup_error, NativeSimError):
                    startup_error.details = {
                        "startup": startup_error.details,
                        "cleanup_error": str(cleanup_error),
                    }
                raise startup_error from cleanup_error
            raise

    def _start(self) -> None:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        environment = os.environ.copy()
        dotnet_root = REPOSITORY_ROOT / ".tools" / "dotnet9"
        if not dotnet_root.is_dir():
            dotnet_root = REPOSITORY_ROOT.parent / ".tools" / "dotnet9"
        if dotnet_root.is_dir():
            environment["DOTNET_ROOT"] = str(dotnet_root)
            environment["DOTNET_ROOT_X64"] = str(dotnet_root)
            environment["PATH"] = str(dotnet_root) + os.pathsep + environment.get("PATH", "")
            environment["DOTNET_ROLL_FORWARD"] = "Major"
        self.process = _spawn_without_windows_error_dialogs(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            creationflags=flags,
            env=environment,
        )
        threading.Thread(target=self._drain_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stdout(self) -> None:
        assert self.process.stdout
        try:
            for line in self.process.stdout:
                self._stdout_lines.put(line)
        finally:
            self._stdout_lines.put(None)

    def _drain_stderr(self) -> None:
        assert self.process.stderr
        for line in self.process.stderr:
            self._logs.append(line.rstrip())

    def _reap_process(self) -> None:
        try:
            self.process.kill()
        except Exception:
            pass
        try:
            self.process.wait(timeout=5)
        except Exception:
            pass
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            if pipe is not None and not pipe.closed:
                try:
                    pipe.close()
                except Exception:
                    pass

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        with self._lock:
            if self.process.poll() is not None:
                raise NativeSimError("worker_crashed", f"worker exited {self.process.returncode}", list(self._logs))
            request_id = uuid.uuid4().hex
            assert self.process.stdin
            self.process.stdin.write(json.dumps({"id": request_id, "method": method, "params": params or {}}, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
            deadline = time.monotonic() + self.request_timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._reap_process()
                    raise NativeSimError(
                        "request_timeout",
                        f"{method} did not respond within {self.request_timeout:.1f} seconds",
                        list(self._logs),
                    )
                try:
                    line = self._stdout_lines.get(timeout=remaining)
                except queue.Empty:
                    self._reap_process()
                    raise NativeSimError(
                        "request_timeout",
                        f"{method} did not respond within {self.request_timeout:.1f} seconds",
                        list(self._logs),
                    ) from None
                if line is None:
                    raise NativeSimError("worker_crashed", f"worker exited {self.process.poll()}", list(self._logs))
                try:
                    response = json.loads(line)
                except json.JSONDecodeError:  # Godot's engine banner is not protocol output.
                    self._logs.append(line.rstrip())
                    continue
                if not isinstance(response, dict) or "id" not in response:
                    continue
                if response["id"] != request_id:
                    self._reap_process()
                    raise NativeSimError(
                        "protocol_desync",
                        f"Response ID mismatch: expected '{request_id}', got '{response['id']}'",
                        {"expected_id": request_id, "actual_id": response["id"], "logs": list(self._logs)},
                    )
                if not response.get("ok"):
                    error = response.get("error") or {}
                    code = error.get("code", "unknown")
                    if code in {"worker_poisoned", "unsafe_transition_abandon", "replay_divergence", "protocol_desync"}:
                        self._reap_process()
                    raise NativeSimError(code, error.get("message", "unknown error"), error.get("details"))
                return response.get("result")

    def hello(self) -> dict[str, Any]: return self.request("hello")
    def catalog(self) -> dict[str, Any]: return self.request("catalog")
    def reset(self, state: dict[str, Any]) -> dict[str, Any]:
        result = self.request("reset", state); self._record_reset("reset", state, state, result)
        return result
    def run_reset(self, state: dict[str, Any]) -> dict[str, Any]:
        result = self.request("run_reset", state); self._record_reset("run_reset", state, state, result)
        return result
    def map_reset(self, state: dict[str, Any]) -> dict[str, Any]:
        result = self.request("map_reset", state); self._record_reset("map_reset", state, state, result)
        return result
    def reward_reset(self, state: dict[str, Any]) -> dict[str, Any]:
        result = self.request("reward_reset", state); self._record_reset("reward_reset", state, state, result)
        return result
    def item_reward_reset(self, state: dict[str, Any], reward_kind: str, model_id: str | None = None) -> dict[str, Any]:
        params = {"state": state, "reward_kind": reward_kind}
        if model_id is not None:
            params["model_id"] = model_id
        result = self.request("item_reward_reset", params); self._record_reset("item_reward_reset", params, state, result)
        return result
    def custom_reward_reset(self, state: dict[str, Any], reward_kinds: list[str], linked: bool = False) -> dict[str, Any]:
        params = {"state": state, "reward_kinds": reward_kinds, "linked": linked}; result = self.request("custom_reward_reset", params); self._record_reset("custom_reward_reset", params, state, result)
        return result
    def rest_reset(self, state: dict[str, Any]) -> dict[str, Any]:
        result = self.request("rest_reset", state); self._record_reset("rest_reset", state, state, result)
        return result
    def event_reset(self, state: dict[str, Any], event_id: str) -> dict[str, Any]:
        params = {"state": state, "event_id": event_id}; result = self.request("event_reset", params); self._record_reset("event_reset", params, state, result)
        return result
    def _record_reset(self, method: str, params: dict[str, Any], state: dict[str, Any], result: dict[str, Any]) -> None:
        self._reset_request = {"method": method, "params": copy.deepcopy(params)}
        self._reset_state = copy.deepcopy(state); self._history = []; self._remember_handle(result["state_handle"])
    def observe(self) -> dict[str, Any]: return self.request("observe")
    def observe_agent(self) -> dict[str, Any]:
        from .observations import extract_agent_observation
        return extract_agent_observation(self.observe())
    def run_observe(self) -> dict[str, Any]: return self.request("run_observe")
    def map_observe(self) -> dict[str, Any]: return self.request("map_observe")
    def reward_observe(self) -> dict[str, Any]: return self.request("reward_observe")
    def rest_observe(self) -> dict[str, Any]: return self.request("rest_observe")
    def event_observe(self) -> dict[str, Any]: return self.request("event_observe")
    def custom_reward_observe(self) -> dict[str, Any]: return self.request("custom_reward_observe")
    def legal_actions(self) -> list[dict[str, Any]]: return self.request("legal_actions")
    def diagnostics(self) -> dict[str, Any]: return self.request("diagnostics")
    def step(self, action_id: str) -> dict[str, Any]:
        result = self.request("step", {"action_id": action_id}); self._history.append(action_id); self._remember_handle(result["state_handle"])
        return result
    def run_step(self, action_id: str) -> dict[str, Any]:
        result = self.request("run_step", {"action_id": action_id}); self._history.append(action_id); self._remember_handle(result["state_handle"])
        return result
    def map_step(self, action_id: str) -> dict[str, Any]:
        result = self.request("map_step", {"action_id": action_id}); self._history.append(action_id); self._remember_handle(result["state_handle"])
        return result
    def reward_step(self, action_id: str) -> dict[str, Any]:
        result = self.request("reward_step", {"action_id": action_id}); self._history.append(action_id); self._remember_handle(result["state_handle"])
        return result
    def rest_step(self, action_id: str) -> dict[str, Any]:
        result = self.request("rest_step", {"action_id": action_id}); self._history.append(action_id); self._remember_handle(result["state_handle"])
        return result
    def event_step(self, action_id: str) -> dict[str, Any]:
        result = self.request("event_step", {"action_id": action_id}); self._history.append(action_id); self._remember_handle(result["state_handle"])
        return result
    def custom_reward_step(self, action_id: str) -> dict[str, Any]:
        result = self.request("custom_reward_step", {"action_id": action_id}); self._history.append(action_id); self._remember_handle(result["state_handle"])
        return result
    def fork(self) -> str:
        handle = self.request("fork")["state_handle"]; self._remember_handle(handle); return handle
    def restore(self, state_handle: str) -> dict[str, Any]:
        if state_handle not in self._handle_histories:
            raise NativeSimError("unknown_local_handle_history", f"State handle '{state_handle}' was evicted or not known locally.")
        result = self.request("restore", {"state_handle": state_handle})
        self._history = list(self._handle_histories[state_handle])
        self._remember_handle(result["state_handle"])
        return result

    def _remember_handle(self, handle: str) -> None:
        self._handle_histories[handle] = list(self._history); self._handle_histories.move_to_end(handle)
        while len(self._handle_histories) > 8192: self._handle_histories.popitem(last=False)

    def export_branch(self) -> dict[str, Any]:
        if self._reset_state is None or self._reset_request is None: raise NativeSimError("not_reset", "Call reset before exporting a branch")
        state = self.observe()
        return {"reset": copy.deepcopy(self._reset_state), "reset_request": copy.deepcopy(self._reset_request), "history": list(self._history), "expected_hash": state["state_hash"]}

    @property
    def memory_bytes(self) -> int:
        try:
            import ctypes
            from ctypes import wintypes
            class PMC(ctypes.Structure):
                _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong), ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t)] + [(f"x{i}", ctypes.c_size_t) for i in range(7)]
            counters = PMC(); counters.cb = ctypes.sizeof(counters)
            function = ctypes.windll.psapi.GetProcessMemoryInfo
            function.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
            function.restype = wintypes.BOOL
            function(wintypes.HANDLE(self.process._handle), ctypes.byref(counters), counters.cb)
            return int(counters.WorkingSetSize)
        except Exception:
            return 0

    def close(self) -> None:
        failure: Exception | None = None
        if self.process.poll() is None:
            try:
                self.request("close")
            except Exception as error:
                failure = error
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
                failure = failure or NativeSimError("worker_shutdown_timeout", "worker did not exit within five seconds", list(self._logs))
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            if pipe is not None and not pipe.closed:
                pipe.close()
        if self.process.returncode not in (None, 0):
            failure = failure or NativeSimError("worker_unclean_exit", f"worker exited {self.process.returncode}", list(self._logs))
        if failure is not None:
            raise failure

    def __enter__(self) -> "NativeWorker": return self
    def __exit__(self, *_: object) -> None: self.close()


class NativeWorkerPool:
    """Fixed pool of isolated persistent workers with crash replacement."""

    def __init__(self, workers: int = 4, **worker_options: Any):
        self.worker_options = worker_options
        self.workers = [NativeWorker(**worker_options) for _ in range(workers)]
        self._executor = ThreadPoolExecutor(max_workers=workers)

    def _replace_if_dead(self, index: int) -> NativeWorker:
        if self.workers[index].process.poll() is not None:
            self.workers[index] = NativeWorker(**self.worker_options)
        return self.workers[index]

    def map(self, operation: Callable[[NativeWorker, Any], Any], values: Iterable[Any]) -> list[Any]:
        values = list(values)
        if len(values) > len(self.workers):
            raise ValueError("one concurrent operation per worker is supported")
        futures = [self._executor.submit(operation, self._replace_if_dead(i), value) for i, value in enumerate(values)]
        return [f.result() for f in futures]

    def reset_all(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        return self.map(lambda worker, value: worker.reset(value), [state] * len(self.workers))

    def restore_portable(self, worker_index: int, branch: dict[str, Any]) -> dict[str, Any]:
        worker = self._replace_if_dead(worker_index)
        reset_request = branch.get("reset_request", {"method": "reset", "params": branch["reset"]})
        method, params = reset_request["method"], reset_request["params"]
        result = worker.request(method, params)
        state = params.get("state", params)
        worker._record_reset(method, params, state, result)
        for action_id in branch["history"]: result = worker.step(action_id)
        if result["state_hash"] != branch["expected_hash"]:
            raise NativeSimError("replay_divergence", f"portable branch expected {branch['expected_hash']}, obtained {result['state_hash']}")
        return result

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)
        failures: list[Exception] = []
        for worker in self.workers:
            try:
                worker.close()
            except Exception as error:
                failures.append(error)
        if failures:
            raise NativeSimError("pool_unclean_exit", f"{len(failures)} worker(s) failed clean shutdown", [str(error) for error in failures])

    def __enter__(self) -> "NativeWorkerPool": return self
    def __exit__(self, *_: object) -> None: self.close()
