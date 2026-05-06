"""
Launcher unificado da federação.

Uso:
    python federation/launch_all.py [--groups grupo_i googlemeet ...]
    python federation/launch_all.py --list
    python federation/launch_all.py --all-local           # ignora host filter
    python federation/launch_all.py --host 192.168.1.10   # finge ser esse host

Multi-PC: o launcher só sobe processos cujo `host` em ports.yaml bate com
o host local (hostname / IPs das interfaces). Em cada PC, rodar este mesmo
launcher — cada um sobe seu(s) grupo(s).
"""

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
FED_DIR = Path(__file__).parent
PORTS_FILE = FED_DIR / "ports.yaml"

CONFLICTING_STANDALONE = set()  # ambos agora aceitam portas via args


def get_local_hosts(extra: str | None = None) -> set[str]:
    """Conjunto de aliases que identificam o PC atual."""
    hosts = {"localhost", "127.0.0.1", "0.0.0.0", "*", "", None}
    try:
        hostname = socket.gethostname()
        hosts.add(hostname)
        hosts.add(hostname.lower())
        for info in socket.getaddrinfo(hostname, None):
            hosts.add(info[4][0])
    except OSError:
        pass
    if extra:
        hosts.add(extra)
        hosts.add(extra.lower())
    return {h.lower() if isinstance(h, str) else h for h in hosts}


def is_local(host: str | None, local_hosts: set[str]) -> bool:
    if not host:
        return True
    return host.lower() in local_hosts


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


def kill_previous(cfg: dict, local_hosts: set[str], run_fed_registry: bool, run_super: bool):
    """Libera portas locais da federação antes de subir.
    Só mata portas de serviços que vão rodar neste PC."""
    own_pid = os.getpid()
    ports = set()
    if run_fed_registry:
        ports.add(cfg["fed_registry_port"])
    if run_super:
        sb = cfg["super_broker"]
        ports.update([sb["txt_xsub"], sb["txt_xpub"], sb["aud_xsub"],
                      sb["aud_xpub"], sb["vid_xsub"], sb["vid_xpub"]])
    for gcfg in cfg["groups"].values():
        if not is_local(gcfg.get("host"), local_hosts):
            continue
        if gcfg.get("registry_port"):
            ports.add(gcfg["registry_port"])
        for ch in ("xpub_txt", "xpub_aud", "xpub_vid"):
            if gcfg.get(ch):
                ports.add(gcfg[ch])
        for p in gcfg.get("extra_kill_ports", []):
            ports.add(p)

    print(f"[launch] Liberando {len(ports)} portas locais de execuções anteriores...")
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


def _expand(cmd: str, gcfg: dict) -> str:
    """Substitui {host} pelo host do grupo. Útil pro broker advertir IP real
    em multi-PC sem precisar duplicar a string em ports.yaml."""
    if not cmd:
        return cmd
    host = gcfg.get("host", "localhost")
    return cmd.replace("{host}", host)


def launch_group(name: str, cfg: dict, processes: list):
    group_dir = ROOT / cfg["dir"]
    raw_env = cfg.get("env") or {}
    env = {k: _expand(str(v), cfg) for k, v in raw_env.items()}
    launch = cfg["launch"]

    if launch.get("registry"):
        p = start_process(f"{name}-registry", _expand(launch["registry"], cfg), group_dir, env)
        processes.append((f"{name}-registry", p))
        reg_port = cfg.get("registry_port")
        if reg_port:
            ok = wait_port(reg_port, timeout=8)
            print(f"  {'✓' if ok else '✗ (timeout)'} registry porta {reg_port}")
        else:
            time.sleep(1.5)

    if launch.get("broker"):
        p = start_process(f"{name}-broker", _expand(launch["broker"], cfg), group_dir, env)
        processes.append((f"{name}-broker", p))
        xpub = cfg.get("xpub_txt")
        if xpub:
            ok = wait_port(xpub, timeout=10)
            print(f"  {'✓' if ok else '✗ (timeout)'} broker xpub_txt porta {xpub}")
        else:
            time.sleep(2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--groups", nargs="*", help="Grupos a subir (padrão: todos locais)")
    parser.add_argument("--list", action="store_true", help="Lista grupos disponíveis")
    parser.add_argument("--no-super", action="store_true", help="Não sobe o SuperBroker")
    parser.add_argument("--no-fed-registry", action="store_true",
                        help="Não sobe o FedRegistry (use se outro PC já hospeda)")
    parser.add_argument("--all-local", action="store_true",
                        help="Ignora campo host: trata todos os grupos como locais (modo single-PC)")
    parser.add_argument("--host", help="Alias extra a considerar como host local")
    args = parser.parse_args()

    cfg = load_ports()
    groups = cfg["groups"]

    if args.list:
        for name, gcfg in groups.items():
            host = gcfg.get("host", "localhost")
            print(f"  {name:20s}  host={host:20s}  dir={gcfg['dir']}  registry={gcfg.get('registry_port')}  xpub_txt={gcfg.get('xpub_txt')}")
        return

    local_hosts = get_local_hosts(args.host)
    fed_reg_host = cfg.get("fed_registry_host", "localhost")
    super_host = cfg.get("super_broker_host", "localhost")

    run_fed_registry = (not args.no_fed_registry) and (args.all_local or is_local(fed_reg_host, local_hosts))
    run_super = (not args.no_super) and (args.all_local or is_local(super_host, local_hosts))

    if args.groups:
        selected = set(args.groups)
    else:
        selected = {n for n, gcfg in groups.items()
                    if args.all_local or is_local(gcfg.get("host"), local_hosts)}

    if not selected and not run_super and not run_fed_registry:
        print("[launch] Nenhum serviço deste host na ports.yaml.")
        print(f"        Hosts locais detectados: {sorted(h for h in local_hosts if h)}")
        print(f"        Use --all-local pra rodar tudo localmente, ou --host <alias>.")
        return

    active_standalone = selected & CONFLICTING_STANDALONE
    if len(active_standalone) > 1:
        print(f"[AVISO] {active_standalone} têm portas hardcoded que conflitam.")
        print("        Suba apenas um deles ou aplique o patch de porta.")
        selected -= set(list(active_standalone)[1:])

    kill_hosts = local_hosts
    if args.all_local:
        kill_hosts = local_hosts | {gcfg.get("host", "localhost").lower() for gcfg in groups.values()}
    kill_previous(cfg, kill_hosts, run_fed_registry, run_super)

    processes = []

    if run_fed_registry:
        p = start_process("fed-registry", f'python "{FED_DIR / "registry_fed.py"}"', ROOT, {})
        processes.append(("fed-registry", p))
        ok = wait_port(cfg["fed_registry_port"], timeout=6)
        print(f"  {'✓' if ok else '✗'} fed-registry porta {cfg['fed_registry_port']}")
    else:
        print(f"[launch] FedRegistry remoto em {fed_reg_host}:{cfg['fed_registry_port']} (não subindo aqui)")

    for name in groups:
        if name not in selected:
            continue
        gcfg = groups[name]
        if not args.all_local and not is_local(gcfg.get("host"), local_hosts):
            print(f"\n[{name}] remoto em {gcfg.get('host')} — pulando")
            continue
        print(f"\n[{name}]")
        try:
            launch_group(name, gcfg, processes)
        except Exception as e:
            print(f"  ✗ erro ao subir {name}: {e}")

    if run_super:
        print("\n[super_broker]")
        time.sleep(1)
        p = start_process("super-broker", f'python "{FED_DIR / "super_broker.py"}"', ROOT, {})
        processes.append(("super-broker", p))
        sb = cfg["super_broker"]
        ok = wait_port(sb["txt_xpub"], timeout=8)
        print(f"  {'✓' if ok else '✗'} super-broker porta {sb['txt_xpub']}")
    elif not args.no_super:
        print(f"[launch] SuperBroker remoto em {super_host} (não subindo aqui)")

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
