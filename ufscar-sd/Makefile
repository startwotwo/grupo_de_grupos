.PHONY: install install-full test demo demo-gui discovery broker1 broker2 broker3 client client-gui kill kill-gui \
	comunicacao-brokers falha-broker reconexao

install:
	pip3 install -r requirements.txt

install-full:
	pip3 install -r requirements-full.txt

test:
	python3 -m pytest tests/ -v

demo:
	python3 examples/demo_cluster_failover.py

discovery:
	python3 examples/start_discovery.py

broker1:
	python3 examples/start_broker_cluster.py 1

broker2:
	python3 examples/start_broker_cluster.py 2

broker3:
	python3 examples/start_broker_cluster.py 3

client:
	python3 examples/start_client_with_failover.py $(USER_ID)

client-gui:
	python3 examples/start_client_gui.py $(USER_ID)

demo-gui:
	python3 examples/start_broker.py &
	sleep 1
	python3 examples/start_client_gui.py alice &
	sleep 0.5
	python3 examples/start_client_gui.py bob

kill:
	pkill -9 -f "demo_cluster_failover" 2>/dev/null || true
	pkill -9 -f "start_broker_cluster" 2>/dev/null || true
	pkill -9 -f "start_discovery" 2>/dev/null || true
	pkill -9 -f "start_client_with_failover" 2>/dev/null || true
	sleep 1

kill-gui:
	pkill -9 -f "start_broker.py" 2>/dev/null || true
	pkill -9 -f "start_client_gui.py" 2>/dev/null || true

comunicacao-brokers:
	pkill -9 -f "demo_" 2>/dev/null || true
	sleep 1
	bash scripts/comunicacao-entre-brokers.sh

falha-broker:
	pkill -9 -f "demo_" 2>/dev/null || true
	sleep 1
	bash scripts/falha-de-broker.sh

reconexao:
	pkill -9 -f "demo_" 2>/dev/null || true
	sleep 1
	bash scripts/reconexao-automatica.sh
