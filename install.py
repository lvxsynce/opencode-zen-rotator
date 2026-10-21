#!/usr/bin/env python3
"""Interactive Linux installer. Does not edit the main OpenCode config."""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from zen_rotator.config import Settings, config_home, validate_route

MARKER_START = "# >>> opencode-zen-rotator >>>"
MARKER_END = "# <<< opencode-zen-rotator <<<"


def ask(message, default=True):
    answer = input(message + (" [Y/n]: " if default else " [y/N]: ")).strip().lower()
    return default if not answer else answer in {"y", "yes", "д", "да"}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def atomic_write(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Refusing to overwrite a symlink: " + str(path))
    fd, name = tempfile.mkstemp(prefix=".zen-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def strip_alias(text):
    if MARKER_START not in text:
        return text
    if text.count(MARKER_START) != 1 or text.count(MARKER_END) != 1:
        raise ValueError("Alias markers are damaged; fix them before continuing")
    start = text.index(MARKER_START)
    end = text.index(MARKER_END) + len(MARKER_END)
    if end <= start:
        raise ValueError("Alias markers are out of order")
    return text[:start] + text[end:].lstrip("\n")


def alias_text(text, launcher):
    clean = strip_alias(text)
    for line in clean.splitlines():
        if line.strip().startswith(("alias opencode=", "function opencode", "opencode()", "opencode ()")):
            raise ValueError("An opencode alias or function already exists; keep it or remove it yourself")
    return clean.rstrip("\n") + "\n" + MARKER_START + "\nalias opencode=" + shlex.quote(shlex.quote(str(launcher))) + "\n" + MARKER_END + "\n"


def find_opencode(custom=None):
    candidates = [custom] if custom else [shutil.which("opencode"), str(Path.home() / ".opencode/bin/opencode")]
    for item in candidates:
        if not item:
            continue
        path = Path(item).expanduser().resolve()
        if path.is_file() and os.access(path, os.X_OK) and path.name != "opencode-zen":
            return path
    raise ValueError("OpenCode not found. Install it first or use --opencode /full/path/opencode")


def free_port(port):
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def health(port):
    # Do not send the local diagnostic request through environment proxies.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/health", timeout=2) as response:
            result = json.load(response)
        return result if isinstance(result, dict) else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def systemd_quote(value):
    text = str(value)
    if any(c in text for c in "\n\r\x00"):
        raise ValueError("Invalid path")
    return '"' + text.replace('\\', '\\\\').replace('"', '\"').replace('%', '%%').replace('$', '$$') + '"'


def unit_text(python, config, mode):
    extra = "" if mode == "user" else "\n[Install]\nWantedBy=multi-user.target\n"
    if mode == "user":
        extra = "\n[Install]\nWantedBy=default.target\n"
    return ("# Managed by opencode-zen-rotator\n[Unit]\nDescription=OpenCode Zen free route gateway\n"
            "After=network-online.target\n\n[Service]\nType=simple\n"
            f"ExecStart={systemd_quote(python)} -m zen_rotator --config {systemd_quote(config)}\n"
            "Restart=on-failure\nRestartSec=3\nUMask=0077\nNoNewPrivileges=true\n" + extra)


def launcher_text(binary):
    # Only TUI/run need their own server; do not break auth, models or help.
    command = shlex.quote(str(binary))
    return '\n'.join([
        '#!/bin/sh', '# Managed by opencode-zen-rotator', 'set -eu',
        'NO_PROXY=localhost,127.0.0.1,::1${NO_PROXY:+,$NO_PROXY}',
        'no_proxy=localhost,127.0.0.1,::1${no_proxy:+,$no_proxy}',
        'export NO_PROXY no_proxy',
        'for arg in "$@"; do',
        '  case "$arg" in --standalone|--server|--server=*) exec ' + command + ' "$@" ;; esac',
        'done',
        'case ${1-} in',
        '  \'\'|run) exec ' + command + ' "$@" --standalone ;;',
        '  *) exec ' + command + ' "$@" ;;',
        'esac', '',
    ])


class Transaction:
    """Save pre-install versions and restore changes if installation fails."""
    def __init__(self, directory):
        self.directory = directory
        self.records = []

    def write(self, path, data, mode=0o600):
        path = Path(path)
        if path.is_symlink():
            raise ValueError("Refusing to replace symlink: " + str(path))
        old = path.read_bytes() if path.exists() else None
        old_mode = path.stat().st_mode & 0o777 if old is not None else None
        backup = None
        if old is not None:
            backup = self.directory / (str(len(self.records)) + ".bak")
            atomic_write(backup, old)
        record = {"path": str(path), "backup": str(backup) if backup else None, "old_mode": old_mode, "sha256": sha(data)}
        self.records.append(record)
        atomic_write(path, data, mode)

    def rollback(self):
        for record in reversed(self.records):
            path = Path(record["path"])
            if record["backup"]:
                atomic_write(path, Path(record["backup"]).read_bytes(), record["old_mode"])
            else:
                path.unlink(missing_ok=True)


def run(command):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if result.returncode:
        # Captured pip/systemd output may contain local environment details.
        raise RuntimeError("Command failed: " + Path(command[0]).name + " (exit " + str(result.returncode) + "). Check dependencies or service permissions.")
    return result.stdout


def systemctl(mode, *args):
    return run(["systemctl"] + (["--user"] if mode == "user" else []) + list(args))


def settings_prompt(port, old=None):
    if old and ask("Оставить текущий список маршрутов?"):
        return Settings(tuple(old["routes"]), old["token"], port)
    routes = ["direct"] if ask("Добавить прямой выход с машины?") else []
    print("Добавь прокси в виде socks5://USER:PASSWORD@proxy.example:1080. Ввод скрыт; пустая строка — закончить.")
    while True:
        route = getpass.getpass("Прокси: ").strip()
        if not route:
            break
        try:
            validate_route(route)
            if route not in routes:
                routes.append(route)
        except ValueError as error:
            print(error)
    return Settings(tuple(routes), old["token"] if old else secrets.token_urlsafe(32), port)


def load_state(path):
    if path.exists():
        return json.loads(path.read_text())
    return None


def install(args):
    if not sys.platform.startswith("linux") or sys.version_info < (3, 10):
        raise ValueError("Installer needs Linux and Python 3.10 or newer")
    base = args.install_dir.expanduser().resolve()
    state_path = base / "install-state.json"
    old = load_state(state_path)
    if old and not ask("Установка уже есть. Обновить её?"):
        return
    binary = find_opencode(args.opencode or (old or {}).get("opencode"))
    print("OpenCode: " + str(binary))
    version = run([str(binary), "--version"]).strip()
    print(version)
    if "2.1." not in version and not ask("Эта версия не проверена. Продолжить?", False):
        return
    config = config_home() / "opencode-zen-rotator/config.json"
    existing = json.loads(config.read_text()) if config.exists() else None
    if existing and not old:
        raise ValueError("Configuration already exists without install-state.json. Move it aside or restore its installation state first")
    port = int(input(f"Порт gateway [{(existing or {}).get('port', 18998)}]: ").strip() or (existing or {}).get('port', 18998))
    if args.routes_file:
        content = json.loads(args.routes_file.read_text())
        settings = Settings(tuple(content["routes"]), (existing or {}).get("token") or secrets.token_urlsafe(32), port)
    else:
        settings = settings_prompt(port, existing)
    if old:
        info = health(old["port"])
        if info and info.get("active", 0):
            raise ValueError("Есть активные запросы. Дождись их завершения и запусти установщик ещё раз")
    if not free_port(port) and not (old and old["port"] == port and health(port)):
        raise ValueError("Port is in use; choose another port")
    use_service = ask("Установить и запустить сервис?")
    mode = "system" if os.geteuid() == 0 else "user"
    if use_service and not shutil.which("systemctl"):
        print("systemd не найден. Можно запускать gateway вручную.")
        if not ask("Продолжить без сервиса?"):
            return
        use_service = False
    if use_service:
        systemctl(mode, "show-environment")
    if old and old.get("service") and not use_service:
        raise ValueError("Для обновления работающего сервиса оставь установку сервиса включённой")
    plugin = config_home() / "opencode/plugins/zen-rotation/index.ts"
    launcher = Path.home() / ".local/bin/opencode-zen"
    shell = Path(os.environ.get("SHELL", "/bin/bash")).name
    rc = Path.home() / (".zshrc" if shell == "zsh" else ".bashrc")
    add_alias = ask("Добавить алиас opencode в " + str(rc) + "?", False)
    unit = (Path("/etc/systemd/system") if mode == "system" else config_home() / "systemd/user") / "zen-rate-gateway.service"
    owned = {r["path"] for r in (old or {}).get("files", [])}
    for target in [plugin, launcher] + ([unit] if use_service else []):
        if target.exists() and str(target) not in owned:
            raise ValueError("Already exists and is not owned by this installer: " + str(target) + ". Nothing overwritten.")
    new_alias = alias_text(rc.read_text() if rc.exists() else "", launcher) if add_alias else None
    base.mkdir(parents=True, exist_ok=True)
    stamp = str(time.time_ns())
    transaction = Transaction(base / "backups" / stamp)
    service_touched = False
    try:
        release = base / "releases" / stamp
        venv = release / ".venv"
        if not (venv / "bin/python").exists():
            print("Создаю отдельное Python-окружение…")
            run([sys.executable, "-m", "venv", str(venv)])
        # Save package files separately from the checkout, never copy local configs.
        package = release / "package"
        for relative in ["pyproject.toml"] + [str(p.relative_to(ROOT)) for p in (ROOT / "src").rglob("*.py")]:
            transaction.write(package / relative, (ROOT / relative).read_bytes(), 0o644)
        print("Устанавливаю зависимости…")
        run([str(venv / "bin/python"), "-m", "pip", "install", str(package)])
        transaction.write(config, (json.dumps({"routes": list(settings.routes), "token": settings.token, "port": settings.port}, indent=2) + "\n").encode())
        template = (ROOT / "plugin/index.ts.in").read_text()
        text = template.replace("__BASE_URL__", json.dumps(f"http://127.0.0.1:{port}/v1")).replace("__LOCAL_TOKEN__", json.dumps(settings.token))
        transaction.write(plugin, text.encode())
        transaction.write(launcher, launcher_text(binary).encode(), 0o755)
        if new_alias is not None:
            transaction.write(rc, new_alias.encode(), rc.stat().st_mode & 0o777 if rc.exists() else 0o644)
        if use_service:
            transaction.write(unit, unit_text(venv / "bin/python", config, mode).encode(), 0o644)
            systemctl(mode, "daemon-reload")
            if old:
                if (health(old["port"]) or {}).get("active", 0):
                    raise ValueError("New active requests appeared; update cancelled")
                service_touched = True
                systemctl(mode, "restart", unit.name)
            service_touched = True
            systemctl(mode, "enable", "--now", unit.name)
            for _ in range(30):
                if health(port):
                    break
                time.sleep(0.2)
            else:
                raise RuntimeError("Gateway did not start. Run doctor and check the service journal")
        # Keep original backups across updates, but track the latest hashes.
        files = {r["path"]: r for r in (old or {}).get("files", [])}
        for record in transaction.records:
            if record["path"] in files:
                files[record["path"]]["sha256"] = record["sha256"]
            else:
                files[record["path"]] = record
        state = {"files": list(files.values()), "port": port, "service": use_service, "mode": mode, "unit": str(unit), "opencode": str(binary), "config": str(config), "alias_rc": str(rc) if new_alias is not None else (old or {}).get("alias_rc")}
        atomic_write(state_path, json.dumps(state, indent=2).encode())
    except BaseException:
        if service_touched:
            try:
                systemctl(mode, "stop", unit.name)
            except RuntimeError:
                print("Проверь вручную, остановлен ли сервис")
        transaction.rollback()
        if use_service:
            try:
                systemctl(mode, "daemon-reload")
                if old and service_touched:
                    systemctl(mode, "restart", unit.name)
                elif not old and service_touched:
                    systemctl(mode, "disable", unit.name)
            except RuntimeError:
                print("Проверь сервис вручную: автоматический откат сервиса не удался")
        raise
    print("Готово. Команда: " + str(launcher))
    if add_alias:
        print("Открой новый терминал или выполни: source " + shlex.quote(str(rc)))
    if not use_service:
        print("Запусти gateway: " + shlex.join([str(venv / "bin/python"), "-m", "zen_rotator", "--config", str(config)]))
    if ask("Проверить через opencode run?", False):
        subprocess.run([str(launcher), "run", "--model", "opencode/deepseek-v4.1-flash-free", "Reply only with OK."], timeout=120, check=True)


def doctor(args):
    state = load_state(args.install_dir.expanduser().resolve() / "install-state.json")
    if not state:
        raise ValueError("Installation not found. Check --install-dir or run install")
    settings = Settings.load(state["config"])
    print("Config: OK (route credentials hidden)")
    print("OpenCode: " + str(find_opencode(state["opencode"])))
    info = health(settings.port)
    print("Gateway: " + (json.dumps(info) if info else "not reachable; check service or start manually"))
    missing = [r["path"] for r in state["files"] if not Path(r["path"]).exists()]
    print("Missing files: " + (", ".join(missing) if missing else "none"))
    if not info or missing:
        raise ValueError("Some checks failed")


def uninstall(args):
    state_path = args.install_dir.expanduser().resolve() / "install-state.json"
    state = load_state(state_path)
    if not state:
        raise ValueError("Installation not found")
    info = health(state["port"])
    if info and info.get("active", 0):
        raise ValueError("Есть активные запросы. Дождись завершения")
    if not ask("Удалить компоненты этой установки?", False):
        return
    if state["service"]:
        systemctl(state["mode"], "disable", "--now", Path(state["unit"]).name)
    keep_config = ask("Оставить список прокси и резервные копии?")
    leftovers = []
    for record in reversed(state["files"]):
        path = Path(record["path"])
        if str(path) == state["config"] and keep_config:
            continue
        if not path.exists():
            continue
        if path.is_symlink():
            leftovers.append(record)
            continue
        if str(path) == state.get("alias_rc"):
            atomic_write(path, strip_alias(path.read_text()).encode(), path.stat().st_mode & 0o777)
        elif sha(path.read_bytes()) != record["sha256"]:
            print("Оставлен изменённый файл: " + str(path))
            leftovers.append(record)
        elif record["backup"]:
            atomic_write(path, Path(record["backup"]).read_bytes(), record["old_mode"])
        else:
            path.unlink()
    if state["service"]:
        systemctl(state["mode"], "daemon-reload")
    if leftovers:
        state["files"] = leftovers
        state["service"] = False
        atomic_write(state_path, json.dumps(state).encode())
    else:
        state_path.unlink()
    print("Готово. Python-окружение и резервные копии не удалены автоматически")


def main():
    parser = argparse.ArgumentParser(description="Установка ротации Zen free")
    parser.add_argument("action", choices=["install", "update", "doctor", "uninstall"], nargs="?", default="install")
    parser.add_argument("--install-dir", type=Path, default=Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "opencode-zen-rotator")
    parser.add_argument("--opencode")
    parser.add_argument("--routes-file", type=Path, help="Read private routes locally instead of typing them")
    args = parser.parse_args()
    try:
        if args.action == "update" and not (args.install_dir / "install-state.json").exists():
            raise ValueError("Nothing to update; run install first")
        {"install": install, "update": install, "doctor": doctor, "uninstall": uninstall}[args.action](args)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, EOFError) as error:
        # Exception messages from arbitrary external services may contain secrets.
        message = str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__
        print("Ошибка: " + message, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nОтменено", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
