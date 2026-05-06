# client/ui.py
import cv2
import logging
import threading
import queue

log = logging.getLogger(__name__)

class UI:
    def __init__(self, capture_manager=None):
        self.frames = {}
        self._running = False
        self.capture = capture_manager
        self.win_name = "Videoconferência"

    def display_video(self, user_id, frame):
        self.frames[user_id] = frame

    def _on_mouse(self, event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN and self.capture:
            # Botão Mute (Esquerda)
            if 10 < x < 110 and 10 < y < 50:
                self.capture.audio_enabled = not self.capture.audio_enabled
                log.info(f"[UI] Audio: {'LIGADO' if self.capture.audio_enabled else 'MUTADO'}")
            
            # Botão Camera (Direita)
            if 120 < x < 220 and 10 < y < 50:
                self.capture.video_enabled = not self.capture.video_enabled
                log.info(f"[UI] Video: {'LIGADO' if self.capture.video_enabled else 'DESLIGADO'}")

    def _draw_controls(self, frame):
        if not self.capture: return
        
        # Fundo dos botões
        cv2.rectangle(frame, (5, 5), (230, 55), (50, 50, 50), -1)
        
        # Botão Audio
        a_col = (0, 255, 0) if self.capture.audio_enabled else (0, 0, 255)
        a_txt = "MUTE" if self.capture.audio_enabled else "UNMUTE"
        cv2.rectangle(frame, (10, 10), (110, 50), a_col, 2)
        cv2.putText(frame, a_txt, (25, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        
        # Botão Video
        v_col = (0, 255, 0) if self.capture.video_enabled else (0, 0, 255)
        v_txt = "CAM ON" if self.capture.video_enabled else "CAM OFF"
        cv2.rectangle(frame, (120, 10), (220, 50), v_col, 2)
        cv2.putText(frame, v_txt, (135, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

    def _render_loop(self):
        import numpy as np
        log.info("[UI] Renderizador de vídeo iniciado")
        cv2.namedWindow(self.win_name)
        cv2.setMouseCallback(self.win_name, self._on_mouse)
        
        while self._running:
            try:
                if not self.frames:
                    # Tela preta se não houver vídeo ainda
                    display_frame = np.zeros((480, 640, 3), dtype=np.uint8)
                    cv2.putText(display_frame, "Aguardando video...", (180, 240), cv2.FONT_HERSHEY_SIMPLEX, 1, (255,255,255), 2)
                else:
                    user_ids = list(self.frames.keys())
                    user_frames = list(self.frames.values())
                    
                    resized_frames = []
                    for uid, f in zip(user_ids, user_frames):
                        # Garantir que todos tenham a mesma altura (480)
                        f_resized = cv2.resize(f, (640, 480))
                        # Escrever o nome do usuário no vídeo
                        cv2.putText(f_resized, uid, (10, 450), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
                        resized_frames.append(f_resized)
                    
                    # Concatena horizontalmente (lado a lado)
                    display_frame = np.hstack(resized_frames)
                
                self._draw_controls(display_frame)
                cv2.imshow(self.win_name, display_frame)
            except Exception as e:
                log.error(f"[UI] Erro render: {e}")
                break
                
            # OpenCV EXIGE que o waitKey seja chamado para processar os eventos da janela
            if cv2.waitKey(30) & 0xFF == ord('q'):
                break
        cv2.destroyAllWindows()

    def start(self):
        self._running = True
        self._render_loop()

    def stop(self):
        self._running = False

    def prompt(self, text):
        return input(text)
