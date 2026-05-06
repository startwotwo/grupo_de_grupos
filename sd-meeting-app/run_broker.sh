#!/bin/bash
# Uso: ./run_broker.sh <índice> [host]
# Exemplo: ./run_broker.sh 0 127.0.0.1
BROKER_IDX=${1:-0}
BROKER_HOST=${2:-127.0.0.1}
source venv/bin/activate
python3 broker.py "$BROKER_IDX" "$BROKER_HOST"
