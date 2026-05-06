"""
Launcher unificado da federação.

Uso:
    python federation/launch_all.py [--groups grupo_i googlemeet ...]
    python federation/launch_all.py --list
"""

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
FED_DIR = Path(__file__).parent
PORTS_FILE = FED_DIR / "ports.yaml"

CONFLICTING_STANDALONE = set()  # ambos agora aceitam portas via args




def load_ports():
    with open(PORTS_FILE) as f:
        return yaml.safe_load(f)


def wait_port(port: int, timeout: float = 10.0) -> bool:
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def kill_port(port: int, own_pid: int):
    """Mata o processo que está segurando a porta, exceto o próprio launcher."""
    result = subprocess.run(
        f'netstat -ano | findstr ":{port} "',
        shell=True, capture_output=True, text=True
    )
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        if f":{port}" not in parts[1]:
            continue
        try:
            pid = int(parts[-1])
        except ValueError:
            continue
        if pid == own_pid or pid == 0:
            continue
        subprocess.run(f"taskkill /F /PID {pid}", shell=True, capture_output=True)


def kill_previous(cfg: dict):
    """Libera todas as portas da federação antes de subir."""
    own_pid = os.getpid()
    ports = set()
    ports.add(cfg["fed_registry_port"])
    sb = cfg["super_broker"]
    ports.update([sb["txt_xsub"], sb["txt_xpub"], sb["aud_xsub"],
                  sb["aud_xpub"], sb["vid_xsub"], sb["vid_xpub"]])
    for gcfg in cfg["groups"].values():
        if gcfg.get("registry_port"):
            ports.add(gcfg["registry_port"])
        for ch in ("xpub_txt", "xpub_aud", "xpub_vid"):
            if gcfg.get(ch):
                ports.add(gcfg[ch])
        for p in gcfg.get("extra_kill_ports", []):
            ports.add(p)

    print(f"[launch] Liberando {len(ports)} portas de execuções anteriores...")
    for port in ports:
        kill_port(port, own_pid)
    time.sleep(1.5)


def start_process(name: str, cmd: str, cwd: Path, env: dict) -> subprocess.Popen:
    full_env = {**os.environ, **{k: str(v) for k, v in env.items()}}
    print(f"[launch] {name}: {cmd}")
    return subprocess.Popen(
        cmd,
        shell=True,
        cwd=str(cwd),
        env=full_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def launch_group(name: str, cfg: dict, processes: list):
    group_dir = ROOT / cfg["dir"]
    env = cfg.get("env") or {}
    launch = cfg["launch"]

    if launch.get("registry"):
        p = start_process(f"{name}-registry", launch["registry"], group_dir, env)
        processes.append((f"{name}-registry", p))
        reg_port = cfg.get("registry_port")
        if reg_port:
            ok = wait_port(reg_port, timeout=8)
            print(f"  {'✓' if ok else '✗ (timeout)'} registry porta {reg_port}")
        else:
            time.sleep(1.5)

    if launch.get("broker"):
        p = start_process(f"{name}-broker", launch["broker"], group_dir, env)
        processes.append((f"{name}-broker", p))
        xpub = cfg.get("xpub_txt")
        if xpub:
            ok = wait_port(xpub, timeout=10)
            print(f"  {'✓' if ok else '✗ (timeout)'} broker xpub_txt porta {xpub}")
        else:
            time.sleep(2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--groups", nargs="*", help="Grupos a subir (padrão: todos)")
    parser.add_argument("--list", action="store_true", help="Lista grupos disponíveis")
    parser.add_argument("--no-super", action="store_true", help="Não sobe o SuperBroker")
    args = parser.parse_args()

    cfg = load_ports()
    groups = cfg["groups"]

    if args.list:
        for name, gcfg in groups.items():
            print(f"  {name:20s}  dir={gcfg['dir']}  registry={gcfg.get('registry_port')}  xpub_txt={gcfg.get('xpub_txt')}")
        return

    selected = set(args.groups) if args.groups else set(groups.keys())

    active_standalone = selected & CONFLICTING_STANDALONE
    if len(active_standalone) > 1:
        print(f"[AVISO] {active_standalone} têm portas hardcoded que conflitam.")
        print("        Suba apenas um deles ou aplique o patch de porta.")
        selected -= set(list(active_standalone)[1:])

    kill_previous(cfg)

    processes = []

    p = start_process("fed-registry", f'python "{FED_DIR / "registry_fed.py"}"', ROOT, {})
    processes.append(("fed-registry", p))
    ok = wait_port(cfg["fed_registry_port"], timeout=6)
    print(f"  {'✓' if ok else '✗'} fed-registry porta {cfg['fed_registry_port']}")

    for name in groups:
        if name not in selected:
            continue
        gcfg = groups[name]
        print(f"\n[{name}]")
        try:
            launch_group(name, gcfg, processes)
        except Exception as e:
            print(f"  ✗ erro ao subir {name}: {e}")

    if not args.no_super:
        print("\n[super_broker]")
        time.sleep(1)
        p = start_process("super-broker", f'python "{FED_DIR / "super_broker.py"}"', ROOT, {})
        processes.append(("super-broker", p))
        sb = cfg["super_broker"]
        ok = wait_port(sb["txt_xpub"], timeout=8)
        print(f"  {'✓' if ok else '✗'} super-broker porta {sb['txt_xpub']}")

    print("\n=== FEDERAÇÃO ATIVA ===")
    print(f"SuperBroker txt_xpub : {cfg['super_broker']['txt_xpub']}")
    print(f"SuperBroker aud_xpub : {cfg['super_broker']['aud_xpub']}")
    print(f"SuperBroker vid_xpub : {cfg['super_broker']['vid_xpub']}")
    print("Ctrl+C para encerrar tudo.\n")

    def shutdown(sig=None, frame=None):
        print("\n[launch] Encerrando todos os processos...")
        for pname, p in reversed(processes):
            try:
                p.terminate()
                p.wait(timeout=3)
            except Exception:
                p.kill()
            print(f"  parado: {pname}")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    import threading

    def stream_output(pname, proc):
        for line in proc.stdout:
            print(f"[{pname}] {line}", end="")

    for pname, p in processes:
        threading.Thread(target=stream_output, args=(pname, p), daemon=True).start()

    while True:
        time.sleep(1)
        dead = [(n, p) for n, p in processes if p.poll() is not None]
        for n, p in dead:
            print(f"[launch] PROCESSO MORTO: {n} (exit={p.returncode})")
            processes.remove((n, p))


if __name__ == "__main__":
    main()
