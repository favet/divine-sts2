from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .paths import default_sandbox_root, find_game_root


@dataclass
class FullAppClientConfig:
    game_root: str = field(default_factory=lambda: str(find_game_root()))
    sandbox_root: str = field(default_factory=lambda: str(default_sandbox_root()))
    worker_id: int = 0
    port: int = 0
    timeout_seconds: float = 60.0


class FullAppBridgeClient:
    def __init__(self, config: FullAppClientConfig) -> None:
        self.config = config
        self.sandbox_dir = Path(config.sandbox_root) / f"worker_{config.worker_id}"
        self.process: Optional[subprocess.Popen[bytes]] = None
        self.sock: Optional[socket.socket] = None
        self.file_reader = None
        self.file_writer = None
        self.request_id = 0
        self.bound_port = 0

    def prepare_sandbox(self, requested_character: str = "IRONCLAD") -> None:
        game_root = Path(self.config.game_root).resolve()
        if not game_root.exists():
            raise FileNotFoundError(f"Game root not found at {game_root}")

        self.sandbox_dir.mkdir(parents=True, exist_ok=True)

        # Hardlink top-level files if missing
        for item in game_root.iterdir():
            if item.is_file():
                dest = self.sandbox_dir / item.name
                if not dest.exists():
                    try:
                        os.link(item, dest)
                    except OSError:
                        # Hardlinks cannot cross volumes (a common Steam/C: temp layout).
                        # Copy only the top-level launcher/resources as a safe fallback.
                        shutil.copy2(item, dest)

        for required in ("SlayTheSpire2.exe", "SlayTheSpire2.pck"):
            if not (self.sandbox_dir / required).is_file():
                raise FileNotFoundError(f"Sandbox preparation did not produce {required}: {self.sandbox_dir}")

        # Junction heavy directories if missing
        for d in ["controller_config", "data_sts2_windows_x86_64"]:
            src = game_root / d
            dest = self.sandbox_dir / d
            if src.exists() and not dest.exists():
                subprocess.run(
                    f'cmd /c mklink /J "{dest}" "{src}"',
                    shell=True,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

        # Deploy full app bridge mod
        mods_dir = self.sandbox_dir / "mods" / "sts2-full-app-bridge"
        mods_dir.mkdir(parents=True, exist_ok=True)

        # Locate built mod package
        repo_root = Path(__file__).resolve().parent.parent.parent
        mod_package = repo_root / "src" / "Sts2.NativeSim.FullAppBridge" / "bin" / "Release" / "net9.0" / "package"
        dll_src = mod_package / "sts2-full-app-bridge.dll"

        if not dll_src.exists():
            raise FileNotFoundError(f"Bridge DLL not found at {dll_src}. Build Sts2.NativeSim.FullAppBridge first.")

        for pkg_file in mod_package.glob("*"):
            if pkg_file.is_file():
                dest_file = mods_dir / pkg_file.name
                try:
                    shutil.copy2(pkg_file, dest_file)
                except Exception:
                    if not dest_file.exists():
                        raise

        # Setup clean isolated userdata
        userdata_dir = self.sandbox_dir / "userdata"
        saves_dir = userdata_dir / "SlayTheSpire2" / "default" / "1" / "modded" / "profile1" / "saves"
        if saves_dir.exists():
            for f in saves_dir.glob("*.save*"):
                try:
                    f.unlink()
                except Exception:
                    pass

        settings_dir = userdata_dir / "SlayTheSpire2" / "default" / "1"
        settings_dir.mkdir(parents=True, exist_ok=True)

        real_appdata = os.environ.get("APPDATA", "")
        source_settings = Path(real_appdata) / "SlayTheSpire2" / "default" / "1" / "settings.save"
        settings_data = {}
        if source_settings.exists():
            try:
                with open(source_settings, "r", encoding="utf-8") as f:
                    settings_data = json.load(f)
            except Exception:
                pass

        settings_data["mod_settings"] = {"mods_enabled": True, "mod_list": []}
        settings_data["fullscreen"] = False
        settings_data["skip_intro_logo"] = True

        with open(settings_dir / "settings.save", "w", encoding="utf-8") as f:
            json.dump(settings_data, f, indent=2)

    def launch(self, requested_character: str = "IRONCLAD") -> None:
        self.prepare_sandbox(requested_character=requested_character)

        port_file = self.sandbox_dir / "userdata" / "bridge_port.txt"
        if port_file.exists():
            port_file.unlink()

        exe_path = self.sandbox_dir / "SlayTheSpire2.exe"
        log_path = self.sandbox_dir / "full_app.log"

        env = os.environ.copy()
        env["APPDATA"] = str(self.sandbox_dir / "userdata")
        env["LOCALAPPDATA"] = str(self.sandbox_dir / "local_userdata")
        env["STS2_FULL_APP_BRIDGE_PORT"] = str(self.config.port)
        env["STS2_FULL_APP_BRIDGE_PORT_FILE"] = str(port_file)
        env["STS2_FORCE_CHARACTER"] = requested_character

        args = [
            str(exe_path),
            "--headless",
            "--force-steam=off",
            f"--log-file={log_path}",
        ]

        self.process = subprocess.Popen(
            args,
            cwd=str(self.sandbox_dir),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Wait for port file to appear and bind
        start_time = time.time()
        bound_port = 0
        while time.time() - start_time < 30.0:
            if port_file.exists():
                try:
                    text = port_file.read_text(encoding="utf-8").strip()
                    if text:
                        bound_port = int(text)
                        break
                except Exception:
                    pass
            if self.process.poll() is not None:
                raise RuntimeError(f"Process terminated prematurely with code {self.process.returncode}")
            time.sleep(0.05)

        if bound_port == 0:
            raise TimeoutError(f"Worker {self.config.worker_id} timed out waiting for bridge port initialization")

        self.bound_port = bound_port

        # Connect TCP socket
        connected = False
        start_connect = time.time()
        while time.time() - start_connect < 15.0:
            try:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.settimeout(self.config.timeout_seconds)
                self.sock.connect(("127.0.0.1", self.bound_port))
                self.file_reader = self.sock.makefile("r", encoding="utf-8-sig")
                self.file_writer = self.sock.makefile("w", encoding="utf-8")
                connected = True
                break
            except Exception:
                time.sleep(0.05)

        if not connected:
            raise ConnectionError(f"Failed to connect to bridge socket on port {self.bound_port}")

    def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        if self.sock is None or self.file_writer is None or self.file_reader is None:
            raise RuntimeError("Client is not connected")

        self.request_id += 1
        payload = {
            "id": self.request_id,
            "method": method,
            "params": params or {},
        }

        msg = json.dumps(payload) + "\n"
        self.file_writer.write(msg)
        self.file_writer.flush()

        response_line = self.file_reader.readline()
        if not response_line:
            raise EOFError("Remote socket closed connection")

        response = json.loads(response_line)
        if response.get("error"):
            raise RuntimeError(f"Bridge RPC error on {method}: {response['error']}")

        return response.get("result")

    def hello(self) -> Dict[str, Any]:
        return self.call("hello")

    def start_run(self, seed: str = "A1B2C3D4E5", character: str = "IRONCLAD", ascension: int = 0) -> Dict[str, Any]:
        return self.call("start_run", {"seed": seed, "character": character, "ascension": ascension})

    def observe(self) -> Dict[str, Any]:
        return self.call("observe")

    def legal_actions(self) -> List[Dict[str, Any]]:
        return self.call("legal_actions")

    def step(self, action_id: str) -> Dict[str, Any]:
        return self.call("step", {"action_id": action_id})

    def history(self) -> Dict[str, Any]:
        return self.call("history")

    def close(self) -> None:
        try:
            if self.sock is not None:
                self.call("close")
        except Exception:
            pass

        try:
            if self.file_writer is not None:
                self.file_writer.close()
        except Exception:
            pass

        try:
            if self.file_reader is not None:
                self.file_reader.close()
        except Exception:
            pass

        try:
            if self.sock is not None:
                self.sock.close()
        except Exception:
            pass

        self.sock = None
        self.file_reader = None
        self.file_writer = None

        if self.process is not None:
            try:
                self.process.terminate()
                self.process.wait(timeout=2.0)
            except Exception:
                try:
                    self.process.kill()
                    self.process.wait(timeout=2.0)
                except Exception:
                    pass
            self.process = None
