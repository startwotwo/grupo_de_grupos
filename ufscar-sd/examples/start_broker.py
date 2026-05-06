import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from broker import Broker

def main():
    broker = Broker(
        broker_id=os.environ.get("BROKER_ID", "broker1"),
        text_port=int(os.environ.get("BROKER_TEXT_PUB_PORT", 5556)),
        audio_pub_port=int(os.environ.get("BROKER_AUDIO_PUB_PORT", 5557)),
        video_pub_port=int(os.environ.get("BROKER_VIDEO_PUB_PORT", 5558)),
        control_port=int(os.environ.get("BROKER_CONTROL_PORT", 5559)),
        heartbeat_port=int(os.environ.get("BROKER_HEARTBEAT_PORT", 5561)),
        audio_input_port=int(os.environ.get("BROKER_AUDIO_INPUT_PORT", 5562)),
        video_input_port=int(os.environ.get("BROKER_VIDEO_INPUT_PORT", 5563)),
        discovery_host=os.environ.get("DISCOVERY_HOST", "localhost"),
        discovery_port=int(os.environ.get("DISCOVERY_PORT", 5555)),
    )
    try:
        broker.start()
        print("\nBroker running. Press Ctrl+C to stop.\n")
        while True:
            pass
    except KeyboardInterrupt:
        print("\nStopping broker...")
        broker.stop()

if __name__ == "__main__":
    main()
