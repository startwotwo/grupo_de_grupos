import os

BROKER_HOST = os.environ.get("BROKER_HOST", "localhost")
PUBLISH_PORT = int(os.environ.get("PUBLISH_PORT", "5555"))
SUBSCRIBE_PORT = int(os.environ.get("SUBSCRIBE_PORT", "5556"))
AUTH_PORT = int(os.environ.get("AUTH_PORT", "5557"))
VIDEO_CAMERA_INDEX = int(os.environ.get("VIDEO_CAMERA_INDEX", "1"))

# Params Audio
AUDIO_INPUT_DEVICE = 1
AUDIO_OUTPUT_DEVICE = 6
AUDIO_SAMPLE_RATE = 48000
AUDIO_CHANNELS = 1
