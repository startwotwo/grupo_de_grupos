import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import threading
import queue
import time
from typing import Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from client.client import Client

try:
    import cv2
    import numpy as np
    from PIL import Image, ImageTk
    _MEDIA_AVAILABLE = True
except ImportError:
    _MEDIA_AVAILABLE = False


_PLACEHOLDER_COLOR = "#2d2d2d"
_PLACEHOLDER_TEXT_COLOR = "#888888"
_BG = "#1a1a2e"
_PANEL_BG = "#16213e"
_BUTTON_BG = "#0f3460"
_ACCENT = "#e94560"
_TEXT_COLOR = "#eaeaea"
_CHAT_BG = "#0d1b2a"
_VIDEO_W = 320
_VIDEO_H = 240


_CAM_ON_COLOR = "#4caf50"
_CAM_OFF_COLOR = "#888888"


class VideoTile(tk.Frame):
    def __init__(self, parent, label: str, **kwargs):
        super().__init__(parent, bg=_PLACEHOLDER_COLOR, width=_VIDEO_W, height=_VIDEO_H + 44,
                         **kwargs)
        self.pack_propagate(False)

        self._canvas = tk.Canvas(self, width=_VIDEO_W, height=_VIDEO_H,
                                 bg=_PLACEHOLDER_COLOR, highlightthickness=0)
        self._canvas.pack(side=tk.TOP)

        bottom = tk.Frame(self, bg=_PLACEHOLDER_COLOR, height=44)
        bottom.pack(side=tk.BOTTOM, fill=tk.X)
        bottom.pack_propagate(False)

        self._label_var = tk.StringVar(value=label)
        tk.Label(bottom, textvariable=self._label_var, bg=_PLACEHOLDER_COLOR,
                 fg=_TEXT_COLOR, font=("Helvetica", 10, "bold")).pack(side=tk.LEFT, padx=6)

        self._cam_indicator = tk.Label(bottom, text="● CAM", bg=_PLACEHOLDER_COLOR,
                                       fg=_CAM_ON_COLOR, font=("Helvetica", 8))
        self._cam_indicator.pack(side=tk.RIGHT, padx=6)

        self._photo = None
        self._initials = label[:2].upper() if label else "??"
        self._draw_placeholder()

    def _draw_placeholder(self):
        self._canvas.delete("all")
        self._canvas.create_rectangle(0, 0, _VIDEO_W, _VIDEO_H, fill=_PLACEHOLDER_COLOR)
        self._canvas.create_oval(
            _VIDEO_W // 2 - 40, _VIDEO_H // 2 - 40,
            _VIDEO_W // 2 + 40, _VIDEO_H // 2 + 40,
            fill="#3a3a5a", outline=""
        )
        self._canvas.create_text(
            _VIDEO_W // 2, _VIDEO_H // 2,
            text=self._initials, fill=_TEXT_COLOR,
            font=("Helvetica", 28, "bold")
        )

    def update_frame(self, frame):
        if not _MEDIA_AVAILABLE:
            return
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb).resize((_VIDEO_W, _VIDEO_H), Image.NEAREST)
        self._photo = ImageTk.PhotoImage(img)
        self._canvas.create_image(0, 0, anchor=tk.NW, image=self._photo)

    def set_label(self, text: str):
        self._label_var.set(text)
        self._initials = text[:2].upper() if text else "??"

    def set_cam_status(self, active: bool):
        if active:
            self._cam_indicator.config(text="● CAM", fg=_CAM_ON_COLOR)
        else:
            self._cam_indicator.config(text="○ CAM OFF", fg=_CAM_OFF_COLOR)

    def show_placeholder(self):
        self._photo = None
        self._draw_placeholder()


class VideoConferenceApp:
    def __init__(self, client: "Client"):
        self.client = client
        self._update_queue: queue.Queue = queue.Queue()
        self._frame_buffers: Dict[str, object] = {}
        self._video_tiles: Dict[str, VideoTile] = {}
        self._last_video_time: Dict[str, float] = {}
        self._local_cap = None
        self._local_frame = None

        self.root = tk.Tk()
        self.root.title(f"VideoConf — {client.client_id}")
        self.root.configure(bg=_BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self._setup_client_callbacks()

        self.root.after(33, self._ui_loop)

        if _MEDIA_AVAILABLE:
            self._start_local_camera()
            self._local_tile.set_cam_status(True)
        else:
            self._local_tile.set_cam_status(False)

    def _build_ui(self):
        self._build_header()
        content = tk.Frame(self.root, bg=_BG)
        content.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        self._build_video_panel(content)
        self._build_chat_panel(content)
        self._build_controls()

    def _build_header(self):
        header = tk.Frame(self.root, bg=_ACCENT, height=40)
        header.pack(fill=tk.X)
        header.pack_propagate(False)

        tk.Label(header, text="VideoConferência Distribuída",
                 bg=_ACCENT, fg="white", font=("Helvetica", 13, "bold")).pack(side=tk.LEFT, padx=12)

        self._group_label = tk.Label(header, text="Sem grupo",
                                     bg=_ACCENT, fg="white", font=("Helvetica", 11))
        self._group_label.pack(side=tk.RIGHT, padx=12)

        self._users_label = tk.Label(header, text="",
                                     bg=_ACCENT, fg="white", font=("Helvetica", 10))
        self._users_label.pack(side=tk.RIGHT, padx=8)

    def _build_video_panel(self, parent):
        self._video_outer = tk.Frame(parent, bg=_BG)
        self._video_outer.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._local_tile = VideoTile(self._video_outer, f"Você ({self.client.client_id})")
        self._local_tile.pack(padx=4, pady=4, side=tk.LEFT, anchor=tk.NW)

        self._remote_frame = tk.Frame(self._video_outer, bg=_BG)
        self._remote_frame.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)

    def _build_chat_panel(self, parent):
        chat_outer = tk.Frame(parent, bg=_PANEL_BG, width=300)
        chat_outer.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 0))
        chat_outer.pack_propagate(False)

        tk.Label(chat_outer, text="Chat", bg=_PANEL_BG, fg=_TEXT_COLOR,
                 font=("Helvetica", 12, "bold")).pack(pady=(8, 4))

        self._chat_display = scrolledtext.ScrolledText(
            chat_outer, bg=_CHAT_BG, fg=_TEXT_COLOR, font=("Courier", 10),
            state=tk.DISABLED, wrap=tk.WORD, height=20
        )
        self._chat_display.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        input_frame = tk.Frame(chat_outer, bg=_PANEL_BG)
        input_frame.pack(fill=tk.X, padx=8, pady=(4, 8))

        self._chat_input = tk.Entry(input_frame, bg=_CHAT_BG, fg=_TEXT_COLOR,
                                    insertbackground=_TEXT_COLOR, font=("Courier", 10))
        self._chat_input.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._chat_input.bind("<Return>", self._send_chat)

        tk.Button(input_frame, text="Enviar", bg=_BUTTON_BG, fg=_TEXT_COLOR,
                  relief=tk.FLAT, command=self._send_chat).pack(side=tk.RIGHT, padx=(4, 0))

    def _build_controls(self):
        ctrl = tk.Frame(self.root, bg=_PANEL_BG, height=50)
        ctrl.pack(fill=tk.X, padx=8, pady=(0, 8))
        ctrl.pack_propagate(False)

        tk.Label(ctrl, text="Grupo:", bg=_PANEL_BG, fg=_TEXT_COLOR).pack(side=tk.LEFT, padx=(8, 2))

        groups = ["A", "B", "C", "D", "E", "F", "G", "H"]
        self._group_var = tk.StringVar(value="A")
        group_combo = ttk.Combobox(ctrl, textvariable=self._group_var, values=groups, width=4,
                                   state="readonly")
        group_combo.pack(side=tk.LEFT, padx=2)

        tk.Button(ctrl, text="Entrar", bg="#2d7a2d", fg="white", relief=tk.FLAT,
                  command=self._join_group).pack(side=tk.LEFT, padx=4)

        tk.Button(ctrl, text="Sair", bg="#7a2d2d", fg="white", relief=tk.FLAT,
                  command=self._leave_group).pack(side=tk.LEFT, padx=4)

        ttk.Separator(ctrl, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8, pady=6)

        self._cam_btn_text = tk.StringVar(value="📷 Câmera: ON")
        self._cam_btn = tk.Button(ctrl, textvariable=self._cam_btn_text, bg=_BUTTON_BG,
                                  fg=_TEXT_COLOR, relief=tk.FLAT, command=self._toggle_camera)
        self._cam_btn.pack(side=tk.LEFT, padx=4)

        self._mic_btn_text = tk.StringVar(value="🎤 Mic: ON")
        self._mic_btn = tk.Button(ctrl, textvariable=self._mic_btn_text, bg=_BUTTON_BG,
                                  fg=_TEXT_COLOR, relief=tk.FLAT, command=self._toggle_mic)
        self._mic_btn.pack(side=tk.LEFT, padx=4)

        tk.Button(ctrl, text="👥 Usuários", bg=_BUTTON_BG, fg=_TEXT_COLOR, relief=tk.FLAT,
                  command=lambda: self.client.list_users()).pack(side=tk.LEFT, padx=4)

        self._status_label = tk.Label(ctrl, text="Conectado", bg=_PANEL_BG,
                                      fg="#4caf50", font=("Helvetica", 9))
        self._status_label.pack(side=tk.RIGHT, padx=12)

    def _setup_client_callbacks(self):
        self.client.on_text_received = self._on_text_received
        self.client.on_video_received = self._on_video_received
        self.client.on_member_joined = self._on_member_joined
        self.client.on_member_left = self._on_member_left

    def _start_local_camera(self):
        def capture():
            import cv2
            cap = cv2.VideoCapture(0)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, _VIDEO_W)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, _VIDEO_H)
            while getattr(self, '_running', True):
                ret, frame = cap.read()
                if ret and self.client.video_enabled:
                    self._local_frame = frame
                time.sleep(1 / 30)
            cap.release()

        self._running = True
        t = threading.Thread(target=capture, daemon=True)
        t.start()

    def _on_member_joined(self, member_id: str):
        self._update_queue.put(lambda m=member_id: self._ensure_member_tile(m))

    def _on_member_left(self, member_id: str):
        self._update_queue.put(lambda m=member_id: self._handle_member_left(m))

    def _ensure_member_tile(self, member_id: str):
        tile = self._get_or_create_tile(member_id)
        tile.set_cam_status(False)

    def _handle_member_left(self, member_id: str):
        if member_id in self._video_tiles:
            self._video_tiles[member_id].show_placeholder()
            self._video_tiles[member_id].set_cam_status(False)
        self._last_video_time.pop(member_id, None)

    def _on_text_received(self, sender_id: str, text: str, group: Optional[str]):
        group_str = f"[{group}]" if group else "[DM]"
        self._update_queue.put(lambda s=sender_id, t=text, g=group_str: self._append_chat(s, t, g))

    def _on_video_received(self, sender_id: str, frame):
        self._frame_buffers[sender_id] = frame

    def _append_chat(self, sender: str, text: str, group: str):
        self._chat_display.config(state=tk.NORMAL)
        ts = time.strftime("%H:%M")
        self._chat_display.insert(tk.END, f"[{ts}] {group} {sender}: {text}\n")
        self._chat_display.config(state=tk.DISABLED)
        self._chat_display.see(tk.END)

    def _get_or_create_tile(self, sender_id: str) -> VideoTile:
        if sender_id not in self._video_tiles:
            tile = VideoTile(self._remote_frame, sender_id)
            tile.pack(padx=4, pady=4, side=tk.LEFT, anchor=tk.NW)
            self._video_tiles[sender_id] = tile
        return self._video_tiles[sender_id]

    def _ui_loop(self):
        while not self._update_queue.empty():
            try:
                self._update_queue.get_nowait()()
            except queue.Empty:
                break

        if _MEDIA_AVAILABLE and self._local_frame is not None:
            self._local_tile.update_frame(self._local_frame)

        now = time.time()
        for sender_id, frame in list(self._frame_buffers.items()):
            tile = self._get_or_create_tile(sender_id)
            tile.update_frame(frame)
            tile.set_cam_status(True)
            self._last_video_time[sender_id] = now
        self._frame_buffers.clear()

        for sender_id, tile in list(self._video_tiles.items()):
            last_t = self._last_video_time.get(sender_id, 0)
            if last_t > 0 and now - last_t > 2.0:
                tile.show_placeholder()
                tile.set_cam_status(False)

        group_text = f"Grupo: {self.client.current_group}" if self.client.current_group else "Sem grupo"
        self._group_label.config(text=group_text)

        self.root.after(33, self._ui_loop)

    def _join_group(self):
        group = self._group_var.get()
        if not group:
            return

        for sender_id, tile in list(self._video_tiles.items()):
            tile.destroy()
        self._video_tiles.clear()
        self._last_video_time.clear()

        success = self.client.join_group(group)
        if success:
            self._append_chat("sistema", f"Entrou no grupo {group}", "")
            self.client.enable_video()
            self.client.enable_audio()
        else:
            messagebox.showerror("Erro", f"Não foi possível entrar no grupo {group}")

    def _leave_group(self):
        if not self.client.current_group:
            return
        self.client.disable_video()
        self.client.disable_audio()
        self.client.leave_group()

        for sender_id, tile in list(self._video_tiles.items()):
            tile.destroy()
        self._video_tiles.clear()
        self._last_video_time.clear()
        self._append_chat("sistema", "Saiu do grupo", "")

    def _toggle_camera(self):
        if self.client.video_enabled:
            self.client.disable_video()
            self._local_frame = None
            self._cam_btn_text.set("📷 Câmera: OFF")
            self._local_tile.show_placeholder()
            self._local_tile.set_cam_status(False)
        else:
            self.client.enable_video()
            self._cam_btn_text.set("📷 Câmera: ON")
            self._local_tile.set_cam_status(True)

    def _toggle_mic(self):
        if self.client.audio_enabled:
            self.client.disable_audio()
            self._mic_btn_text.set("🎤 Mic: OFF")
        else:
            self.client.enable_audio()
            self._mic_btn_text.set("🎤 Mic: ON")

    def _send_chat(self, event=None):
        text = self._chat_input.get().strip()
        if not text:
            return
        self._chat_input.delete(0, tk.END)

        if not self.client.current_group:
            messagebox.showwarning("Aviso", "Entre em um grupo primeiro.")
            return

        self._append_chat(f"Você ({self.client.client_id})", text,
                          f"[{self.client.current_group}]")
        threading.Thread(target=self.client.send_text, args=(text,), daemon=True).start()

    def _on_close(self):
        self._running = False
        self.client.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()
