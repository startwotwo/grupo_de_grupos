# media/audio_codec.py
RATE       = 16000   # Hz — voz (menor = menor banda)
CHANNELS   = 1       # mono
CHUNK      = 1024    # frames por buffer
FORMAT_STR = "int16" # numpy dtype equivalente ao paInt16


def encode_audio(pcm_bytes: bytes) -> bytes:
    """Por ora retorna raw PCM. Trocar por opus futuramente."""
    return pcm_bytes


def decode_audio(data: bytes) -> bytes:
    return data