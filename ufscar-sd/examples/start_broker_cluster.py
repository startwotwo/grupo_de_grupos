import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from broker import Broker


def main():
    if len(sys.argv) < 2:
        print("Usage: python start_broker_cluster.py <broker_number>")
        print("  broker_number: 1, 2, or 3")
        sys.exit(1)

    broker_num = int(sys.argv[1])

    broker_configs = {
        1: {
            'broker_id': 'broker1',
            'text_port': 5556,
            'control_port': 5559,
            'heartbeat_port': 5561
        },
        2: {
            'broker_id': 'broker2',
            'text_port': 5656,
            'control_port': 5659,
            'heartbeat_port': 5661
        },
        3: {
            'broker_id': 'broker3',
            'text_port': 5756,
            'control_port': 5759,
            'heartbeat_port': 5761
        }
    }

    if broker_num not in broker_configs:
        print(f"Invalid broker number: {broker_num}")
        sys.exit(1)

    config = broker_configs[broker_num]

    broker = Broker(
        broker_id=config['broker_id'],
        text_port=config['text_port'],
        control_port=config['control_port'],
        heartbeat_port=config['heartbeat_port'],
        discovery_host='localhost',
        enable_clustering=True
    )

    try:
        broker.start()

        print(f"\nBroker {broker_num} running. Press Ctrl+C to stop.\n")

        while True:
            pass

    except KeyboardInterrupt:
        print(f"\nStopping broker {broker_num}...")
        broker.stop()


if __name__ == "__main__":
    main()
