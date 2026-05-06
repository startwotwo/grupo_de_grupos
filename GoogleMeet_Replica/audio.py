# tem q instsalar o portaudio19-dev no ubuntu

# Source - https://stackoverflow.com/a/64823047
# Posted by sawyermclane
# Retrieved 2026-04-23, License - CC BY-SA 4.0

#sudo apt install portaudio19-dev


import pyaudio

class Audio:
    def __init__(self):
        self._p = pyaudio.PyAudio()
        self._stream = self._p.open(
            format=pyaudio.paInt16,
            channels=2,
            rate=44100,
            output=True,
            input=True
        )

    def write(self, data: bytes):
        self._stream.write(data)
    
    def read(self, size=1024) -> bytes:
        return self._stream.read(size)


if __name__ == "__main__":
    audio = Audio()

    #while True:
    #    audio.write(audio.read(1024))

    import wave
    with wave.open('test-audio.wav', 'rb') as wf:
        while len(data := wf.readframes(1024)):  # Requires Python 3.8+ for :=
            audio.write(data)