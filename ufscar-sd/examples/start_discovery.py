import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from discovery import DiscoveryService

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.environ.get("DISCOVERY_PORT", 5555)))
    args = parser.parse_args()

    discovery = DiscoveryService(port=args.port)
    try:
        discovery.start()
        print(f"\nDiscovery Service running on {args.port}. Press Ctrl+C to stop.\n")
        while True:
            pass
    except KeyboardInterrupt:
        print("\nStopping discovery service...")
        discovery.stop()

if __name__ == "__main__":
    main()
