#!/bin/bash
# Executa a demonstração completa (registry + brokers + clientes + falha)
source venv/bin/activate
python3 run_demo.py "$@"
