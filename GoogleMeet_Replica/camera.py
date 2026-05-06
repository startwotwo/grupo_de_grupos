from typing import *
import cv2
import numpy as np


class Camera:
    def __init__(self, filename=0):
        self._camera = None
        self.placeholder = None

        try:
            self._camera = cv2.VideoCapture(filename)
            if not self._camera.isOpened():
                raise cv2.error("Camera not available")
        except cv2.error:
            print("[!] Não conseguimo abrir a câmera cumpadi!")
            self._camera = None
            self.placeholder = np.zeros((480, 640, 3), dtype=np.uint8)
            self.placeholder[:] = (255, 0, 0)

    def get_frame(self, username: str = "User") -> Optional[np.ndarray]:
        if self._camera:
            ret, frame = self._camera.read()
            if ret and frame is not None:
                return frame

        if self.placeholder is not None:
            frame = self.placeholder.copy()
            initial = username[0].upper() if username else "U"
            cv2.putText(frame, initial, (260, 260), cv2.FONT_HERSHEY_SIMPLEX,
                        5, (255, 255, 255), 12, cv2.LINE_AA)
            return frame

        return None

    def release(self):
        if self._camera:
            self._camera.release()
            self._camera = None


if __name__ == "__main__":
    camera = Camera()
    frame = camera.get_frame()
    print(type(frame), None if frame is None else frame.shape)