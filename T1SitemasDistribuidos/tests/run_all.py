# tests/run_all.py
import subprocess
import sys
import time
import os

tests = ["test_registry.py", "test_cluster.py", "test_failover.py"]

def run():
    print("=== INICIANDO SUÍTE DE TESTES DO SISTEMA DISTRIBUÍDO ===\n")
    all_success = True
    
    for test in tests:
        print(f"Executando {test}...")
        try:
            # Stream direto para o terminal para evitar buffering e confusão no log
            process = subprocess.Popen(
                [sys.executable, f"tests/{test}"],
                stdout=sys.stdout,
                stderr=sys.stderr,
                text=True
            )
            
            try:
                process.wait(timeout=90) # Aumentado para 90s pois os testes distribuídos são lentos
                if process.returncode == 0:
                    print(f"\n[SUCESSO] {test}")
                else:
                    print(f"\n[FALHA] {test} (Código: {process.returncode})")
                    all_success = False
            except subprocess.TimeoutExpired:
                print(f"\n[TIMEOUT] {test} demorou demais.")
                process.kill()
                all_success = False
                
        except Exception as e:
            print(f"\n[ERRO] Falha ao rodar {test}: {e}")
            all_success = False
            
        print("-" * 50)
        time.sleep(2) # Pausa para limpeza de sockets do SO

    if all_success:
        print("\n>>> TODOS OS TESTES PASSARAM COM SUCESSO! <<<")
        sys.exit(0)
    else:
        print("\n>>> ALGUNS TESTES FALHARAM. <<<")
        sys.exit(1)

if __name__ == "__main__":
    run()
