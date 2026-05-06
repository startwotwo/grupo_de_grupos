#!/bin/bash

# Ensure nothing is running
killall python3 2>/dev/null

echo "==========================================="
echo "   Distributed Video Conference Demo       "
echo "==========================================="

echo "[1] Starting Registry (Port 5555)..."
python3 registry.py > registry.log 2>&1 &
REG_PID=$!
sleep 2

echo "[2] Starting Broker 1 (Ports 6000-)..."
python3 broker.py --port-base 6000 > broker1.log 2>&1 &
B1_PID=$!
sleep 2

echo "[3] Starting Broker 2 (Ports 6100-)..."
python3 broker.py --port-base 6100 > broker2.log 2>&1 &
B2_PID=$!
sleep 2

echo "Infrastructure is up! Logs are written to registry.log, broker1.log, broker2.log."
echo ""
echo "=> To test, open multiple terminals and run:"
echo "python3 client.py --user Alice"
echo "python3 client.py --user Bob"
echo ""
echo "=> To test FAULT TOLERANCE:"
echo "Press ENTER here to crash Broker 1. Then try sending a text message from a client."
echo "The client will detect the failure (timeout) and reconnect to Broker 2 automatically!"

read -p "Press [Enter] to crash Broker 1..."

echo "Crashing Broker 1..."
kill -9 $B1_PID
echo "Broker 1 killed! Clients connected to it will migrate to Broker 2 on their next text message retry."

echo "Press [Enter] to stop everything and exit."
read

kill -9 $REG_PID $B2_PID 2>/dev/null
echo "Demo stopped."
