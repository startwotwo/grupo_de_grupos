#!/usr/bin/env python3
"""
Interface gráfica de videoconferência — sd-meeting-app

Controles disponíveis:
  🎤  Mutar / desmutar microfone
  📷  Ligar / desligar câmera
  🔊  Mutar / desmutar saída de áudio
  🚪  Sair da sala (volta para login)

Tecnologias:
  Tkinter — widgets e layout (built-in, sem dependência extra)
  Pillow  — converte frames OpenCV (numpy) em PhotoImage para Tkinter
  ZeroMQ  — mesma arquitetura do client.py (PUSH/SUB/DEALER)

Uso:
  python3 client_gui.py
"""

import json
import queue
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import uuid

import zmq
from client import _load_cfg as load_config, DiscoveryClient, TextQoS, VideoQoS, GUIClientSession

try:
    from PIL import Image, ImageTk, ImageDraw, ImageFont
    _PIL_OK = True
except ImportError:
    _PIL_OK = False
    print("[GUI] Pillow não instalado — vídeo desabilitado. "
          "Instale com: pip install Pillow")

try:
    import pyaudio
    _AUDIO_OK = True
except ImportError:
    _AUDIO_OK = False

try:
    import cv2
    import numpy as np
    _VIDEO_OK = True and _PIL_OK
except ImportError:
    _VIDEO_OK = False


# ---------------------------------------------------------------------------
# Paleta de cores — tema escuro
# ---------------------------------------------------------------------------

C_BG      = "#1a1a2e"   # fundo principal
C_PANEL   = "#16213e"   # painéis internos
C_SURFACE = "#0f3460"   # superfície de botões / inputs
C_ACCENT  = "#e94560"   # vermelho: muted / câmera off / sair
C_GREEN   = "#00d4aa"   # verde: conectado / ativo
C_TEXT    = "#eaeaea"   # texto principal
C_DIM     = "#7a7a8a"   # texto secundário
C_BORDER  = "#2a2a4e"   # bordas
C_SELF    = "#533483"   # borda do self-preview
C_CHAT_ME = "#00d4aa"   # cor do meu nome no chat
C_CHAT_OT = "#e8c46a"   # cor dos outros no chat

FONT_UI   = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI", 10, "bold")
FONT_MONO = ("Consolas",  9)
FONT_TITLE= ("Segoe UI", 13, "bold")
FONT_SMALL= ("Segoe UI",  8)


# ---------------------------------------------------------------------------
# Painel de vídeo reutilizável
# ---------------------------------------------------------------------------

class VideoPanel(tk.Frame):
    """Exibe frames de vídeo ou um placeholder com texto."""

    PLACEHOLDER_COLOR = "#0d0d1a"

    def __init__(self, parent, width: int, height: int, label: str = "", **kw):
        super().__init__(parent, bg=C_PANEL,
                         highlightbackground=C_BORDER, highlightthickness=1,
                         **kw)
        self.W = width
        self.H = height
        self._label_text = label
        self._photo = None
        self._placeholder = self._make_placeholder("Sem vídeo")

        self._canvas = tk.Canvas(self, width=width, height=height,
                                 bg=self.PLACEHOLDER_COLOR,
                                 highlightthickness=0)
        self._canvas.pack()
        self._img_id = self._canvas.create_image(0, 0, anchor="nw",
                                                  image=self._placeholder)
        if label:
            self._canvas.create_text(
                6, height - 6, anchor="sw",
                text=label, fill=C_DIM, font=FONT_SMALL,
            )

    def _make_placeholder(self, text: str):
        if not _PIL_OK:
            return None
        img = Image.new("RGB", (self.W, self.H), color=self.PLACEHOLDER_COLOR)
        draw = ImageDraw.Draw(img)
        draw.text((self.W // 2, self.H // 2), text,
                  fill=C_DIM, anchor="mm")
        photo = ImageTk.PhotoImage(img)
        return photo

    def show_frame(self, jpeg_bytes: bytes):
        """Atualiza com frame JPEG recebido."""
        if not _PIL_OK or not _VIDEO_OK:
            return
        try:
            arr   = np.frombuffer(jpeg_bytes, dtype=np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if frame is None:
                return
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img   = Image.fromarray(frame_rgb).resize(
                (self.W, self.H), Image.LANCZOS
            )
            photo = ImageTk.PhotoImage(img)
            self._canvas.itemconfigure(self._img_id, image=photo)
            self._photo = photo          # evita coleta pelo GC
        except Exception:
            pass

    def show_camera_frame(self, frame):
        """Atualiza com frame numpy (BGR) da câmera local."""
        if not _PIL_OK:
            return
        try:
            rgb   = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img   = Image.fromarray(rgb).resize(
                (self.W, self.H), Image.LANCZOS
            )
            photo = ImageTk.PhotoImage(img)
            self._canvas.itemconfigure(self._img_id, image=photo)
            self._photo = photo
        except Exception:
            pass

    def show_placeholder(self, text: str = "Sem vídeo"):
        ph = self._make_placeholder(text)
        if ph:
            self._canvas.itemconfigure(self._img_id, image=ph)
            self._placeholder = ph


# ---------------------------------------------------------------------------
# Aplicação principal
# ---------------------------------------------------------------------------

class ConferenceApp:

    # Dimensões dos painéis de vídeo
    REM_W, REM_H = 640, 400   # vídeo remoto principal
    SELF_W, SELF_H = 200, 150  # self-preview
    REM_TILE_W, REM_TILE_H = 308, 180
    REM_TILE_COLS = 2

    def __init__(self):
        self.cfg       = load_config()
        self.client_id = str(uuid.uuid4())
        self.username  = None
        self.room      = None
        self.broker    = None
        self._participant_names: dict[str, str] = {}
        self._video_panels: dict[str, dict[str, object]] = {}

        # Estado dos controles
        self.muted        = False
        self.camera_on    = True
        self.speaker_on   = True

        # Sincronização entre threads e GUI
        self._stop    = threading.Event()
        self._session_stop = threading.Event()
        self._gui_q:  queue.Queue = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._discovery = DiscoveryClient(self.cfg)
        self._reconnect_lock = threading.Lock()
        self._reconnect_in_progress = False

        # Network session
        self._network_session = GUIClientSession(self.cfg, self.client_id,
                                                 self._gui_q, self._session_stop)

        # Audio callback buffering
        self.recv_queue = queue.Queue()  # Buffers received audio frames for output callback
        self.p = None                    # PyAudio instance
        self.audio_stream = None         # Combined input/output stream with callbacks

        # Tkinter
        self.root = tk.Tk()
        self.root.title("sd-meeting")
        self.root.configure(bg=C_BG)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_login()
        self.root.mainloop()

    # ------------------------------------------------------------------
    # Tela de login
    # ------------------------------------------------------------------

    def _build_login(self):
        self.root.geometry("420x340")
        self._login_frame = tk.Frame(self.root, bg=C_BG)
        self._login_frame.pack(fill="both", expand=True)

        # Logo / título
        tk.Label(self._login_frame, text="📹 sd-meeting",
                 font=("Segoe UI", 22, "bold"),
                 fg=C_GREEN, bg=C_BG).pack(pady=(40, 4))
        tk.Label(self._login_frame, text="Videoconferência distribuída com ZeroMQ",
                 font=FONT_SMALL, fg=C_DIM, bg=C_BG).pack(pady=(0, 30))

        form = tk.Frame(self._login_frame, bg=C_BG)
        form.pack()

        def _row(label, widget_fn, **kw):
            r = tk.Frame(form, bg=C_BG)
            r.pack(fill="x", pady=6)
            tk.Label(r, text=label, font=FONT_UI, fg=C_DIM,
                     bg=C_BG, width=10, anchor="e").pack(side="left")
            w = widget_fn(r, **kw)
            w.pack(side="left", padx=(8, 0))
            return w

        self._var_user = tk.StringVar(value="alice")
        self._var_room = tk.StringVar(value="A")

        entry_style = dict(
            bg=C_SURFACE, fg=C_TEXT,
            insertbackground=C_TEXT,
            relief="flat", font=FONT_UI,
            width=20,
        )
        self._entry_user = _row("Usuário:", tk.Entry,
                                textvariable=self._var_user, **entry_style)

        # Combobox de salas
        rooms = self.cfg["cluster"]["all_rooms"]
        room_cb = ttk.Combobox(form, textvariable=self._var_room,
                               values=rooms, width=18, state="readonly",
                               font=FONT_UI)
        r = tk.Frame(form, bg=C_BG)
        r.pack(fill="x", pady=6)
        tk.Label(r, text="Sala:", font=FONT_UI, fg=C_DIM,
                 bg=C_BG, width=10, anchor="e").pack(side="left")
        room_cb.pack(side="left", padx=(8, 0))

        # Status
        self._login_status = tk.Label(self._login_frame, text="",
                                      font=FONT_SMALL, fg=C_ACCENT, bg=C_BG)
        self._login_status.pack(pady=(16, 0))

        # Botão entrar
        btn = tk.Button(
            self._login_frame, text="  Entrar  ", font=FONT_BOLD,
            bg=C_GREEN, fg=C_BG, relief="flat", cursor="hand2",
            activebackground="#00b89a", activeforeground=C_BG,
            command=self._do_login,
        )
        btn.pack(pady=(8, 0))
        self._entry_user.bind("<Return>", lambda e: self._do_login())

    def _do_login(self):
        username = self._var_user.get().strip()
        room     = self._var_room.get().strip().upper()
        if not username:
            self._login_status.config(text="Nome de usuário obrigatório.")
            return
        if room not in self.cfg["cluster"]["all_rooms"]:
            self._login_status.config(text="Sala inválida.")
            return
        self.username = username
        self.room     = room
        self._login_status.config(text="Conectando...", fg=C_DIM)
        self.root.update()
        threading.Thread(target=self._connect_and_launch, daemon=True).start()

    def _connect_and_launch(self):
        self._network_session.set_credentials(self.username, self.room)
        for i in range(15):
            if self._network_session.discover_broker(max_retries=1, delay=0):
                self.broker = self._network_session.broker
                self.root.after(0, self._switch_to_conference)
                return
            self.root.after(0, lambda i=i: self._login_status.config(
                text=f"Aguardando broker... ({i+1}/15)"))
            time.sleep(1)
        self.root.after(0, lambda: self._login_status.config(
            text="Nenhum broker disponível.", fg=C_ACCENT))

    # ------------------------------------------------------------------
    # Tela de conferência
    # ------------------------------------------------------------------

    def _switch_to_conference(self):
        try:
            self._login_frame.destroy()
            self.root.geometry(f"{self.REM_W + self.SELF_W + 260}x"
                               f"{self.REM_H + self.SELF_H + 120}")
            self._build_conference()
            if not self._start_network():
                messagebox.showerror("Erro", "Falha ao conectar à rede")
                return
            self.root.after(33, self._poll_gui_queue)
        except Exception:
            err = traceback.format_exc()
            print(err, file=sys.stderr)
            messagebox.showerror("Erro ao entrar na sala", err)

    def _build_conference(self):
        self._conf_frame = tk.Frame(self.root, bg=C_BG)
        self._conf_frame.pack(fill="both", expand=True)

        # ── Barra superior ─────────────────────────────────────────────
        top = tk.Frame(self._conf_frame, bg=C_PANEL, height=40)
        top.pack(fill="x")
        top.pack_propagate(False)

        tk.Label(top, text=f"[SD-MEETING]  Sala  {self.room}",
                 font=FONT_BOLD, fg=C_GREEN, bg=C_PANEL).pack(side="left", padx=12)
        tk.Label(top, text=f"[{self.username}]",
                 font=FONT_UI, fg=C_TEXT, bg=C_PANEL).pack(side="left", padx=4)

        self._status_lbl = tk.Label(top, text="● Conectando...",
                                    font=FONT_SMALL, fg=C_ACCENT, bg=C_PANEL)
        self._status_lbl.pack(side="left", padx=16)

        tk.Button(top, text="Sair", font=FONT_BOLD,
                  bg=C_ACCENT, fg="white", relief="flat", cursor="hand2",
                  activebackground="#c73652", activeforeground="white",
                  command=self._leave).pack(side="right", padx=10, pady=5)

        # ── Área principal ─────────────────────────────────────────────
        main = tk.Frame(self._conf_frame, bg=C_BG)
        main.pack(fill="both", expand=True)

        # Coluna esquerda: vídeo
        gallery_width = self.REM_TILE_W * self.REM_TILE_COLS + 24
        left = tk.Frame(main, bg=C_BG)
        left.configure(width=gallery_width)
        left.pack_propagate(False)
        left.pack(side="left", fill="both", padx=6, pady=6)

        tk.Label(left, text="Vídeos remotos", font=FONT_BOLD,
             fg=C_TEXT, bg=C_BG).pack(anchor="w", pady=(0, 4))

        self._video_gallery = tk.Frame(left, bg=C_BG, width=gallery_width)
        self._video_gallery.pack(fill="x")

        self._video_self = VideoPanel(left, self.SELF_W, self.SELF_H,
                                      label=f"Você ({self.username})")
        self._video_self.pack(pady=(8, 0))

        # Coluna direita: membros + chat
        right = tk.Frame(main, bg=C_BG)
        right.pack(side="left", fill="both", expand=True, padx=(0, 6), pady=6)

        # Membros
        members_frame = tk.LabelFrame(right, text=" Membros ",
                                      bg=C_PANEL, fg=C_DIM,
                                      font=FONT_SMALL, relief="flat",
                                      highlightbackground=C_BORDER,
                                      highlightthickness=1)
        members_frame.pack(fill="x", pady=(0, 6))

        self._members_list = tk.Listbox(
            members_frame, bg=C_PANEL, fg=C_TEXT,
            selectbackground=C_SURFACE, font=FONT_UI,
            relief="flat", height=5, borderwidth=0,
        )
        self._members_list.pack(fill="x", padx=4, pady=4)

        # Chat
        chat_frame = tk.LabelFrame(right, text=" Chat ",
                                   bg=C_PANEL, fg=C_DIM,
                                   font=FONT_SMALL, relief="flat",
                                   highlightbackground=C_BORDER,
                                   highlightthickness=1)
        chat_frame.pack(fill="both", expand=True)

        self._chat_area = scrolledtext.ScrolledText(
            chat_frame, bg=C_PANEL, fg=C_TEXT,
            font=FONT_MONO, relief="flat",
            state="disabled", wrap="word",
            insertbackground=C_TEXT,
        )
        self._chat_area.pack(fill="both", expand=True, padx=4, pady=4)
        self._chat_area.tag_config("me",    foreground=C_CHAT_ME)
        self._chat_area.tag_config("other", foreground=C_CHAT_OT)
        self._chat_area.tag_config("sys",   foreground=C_DIM,
                                   font=FONT_SMALL)
        self._chat_area.tag_config("ts",    foreground=C_DIM,
                                   font=FONT_SMALL)

        # Input de texto
        inp_row = tk.Frame(chat_frame, bg=C_PANEL)
        inp_row.pack(fill="x", padx=4, pady=(0, 4))

        self._chat_entry = tk.Entry(
            inp_row, bg=C_SURFACE, fg=C_TEXT,
            insertbackground=C_TEXT, font=FONT_UI,
            relief="flat",
        )
        self._chat_entry.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self._chat_entry.bind("<Return>", lambda e: (self._send_text(), "break"))

        tk.Button(inp_row, text="Enviar", font=FONT_BOLD,
                  bg=C_GREEN, fg=C_BG, relief="flat", cursor="hand2",
                  activebackground="#00b89a",
                  command=self._send_text).pack(side="left")

        # ── Barra de controles ─────────────────────────────────────────
        self._build_controls()

    def _build_controls(self):
        bar = tk.Frame(self._conf_frame, bg=C_SURFACE, height=54)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)

        def _ctrl_btn(parent, text, cmd, color=C_TEXT):
            b = tk.Button(parent, text=text, font=("Segoe UI", 11),
                          bg=C_SURFACE, fg=color, relief="flat",
                          cursor="hand2", width=13,
                          activebackground=C_BG, activeforeground=color,
                          command=cmd)
            b.pack(side="left", padx=6, pady=8)
            return b

        center = tk.Frame(bar, bg=C_SURFACE)
        center.pack(expand=True)

        self._btn_mute    = _ctrl_btn(center, "[MIC] Mutar",     self._toggle_mute)
        self._btn_camera  = _ctrl_btn(center, "[CAM] Camera",    self._toggle_camera)
        self._btn_speaker = _ctrl_btn(center, "[SPK] Audio on",  self._toggle_speaker)

        # Indicador de qualidade de vídeo
        self._quality_lbl = tk.Label(bar, text="",
                                     font=FONT_SMALL, fg=C_DIM, bg=C_SURFACE)
        self._quality_lbl.pack(side="right", padx=10)

    # ------------------------------------------------------------------
    # Controles
    # ------------------------------------------------------------------

    def _toggle_mute(self):
        self.muted = not self.muted
        if self.muted:
            self._btn_mute.config(text="[MIC] Mutado", fg=C_ACCENT, bg="#2a1020")
        else:
            self._btn_mute.config(text="[MIC] Mutar",  fg=C_TEXT,   bg=C_SURFACE)

    def _toggle_camera(self):
        self.camera_on = not self.camera_on
        if self.camera_on:
            self._btn_camera.config(text="[CAM] Camera",   fg=C_TEXT,   bg=C_SURFACE)
            self._network_session.set_camera_enabled(True)
        else:
            self._btn_camera.config(text="[CAM] Desligada",fg=C_ACCENT, bg="#2a1020")
            self._video_self.show_placeholder("📷 Câmera desligada")
            self._network_session.set_camera_enabled(False)

    def _toggle_speaker(self):
        self.speaker_on = not self.speaker_on
        if self.speaker_on:
            self._btn_speaker.config(text="[SPK] Audio on",  fg=C_TEXT,   bg=C_SURFACE)
        else:
            self._btn_speaker.config(text="[SPK] Audio off", fg=C_ACCENT, bg="#2a1020")

    def _send_text(self):
        content = self._chat_entry.get().strip()
        if not content:
            return
        self._chat_entry.delete(0, "end")
        self._network_session.send_text(content)
        # Exibe no chat local imediatamente
        self._append_chat(self.username, content, time.time(), is_me=True)

    def _leave(self):
        if messagebox.askyesno("Sair", "Deseja sair da sala?",
                               parent=self.root):
            self._session_stop.set()
            self._network_session.stop()
            self._cleanup_audio()
            self._clear_video_panels()
            self._conf_frame.destroy()
            self._build_login()
            self.root.geometry("420x340")
            self._session_stop = threading.Event()
            self.broker    = None
            self.username  = None
            self.room      = None
            self._participant_names = {}
            self._video_panels = {}
            self._network_session = GUIClientSession(self.cfg, self.client_id,
                                                     self._gui_q, self._session_stop)

    def _on_close(self):
        self._stop.set()
        self._session_stop.set()
        # Cleanup network and audio resources before closing
        if self._network_session:
            self._network_session.stop()
        self._cleanup_audio()
        self._clear_video_panels()
        self.root.destroy()

    # ------------------------------------------------------------------
    # Atualização da GUI (sempre chamado no thread principal via after)
    # ------------------------------------------------------------------

    def _poll_gui_queue(self):
        try:
            while not self._gui_q.empty():
                item = self._gui_q.get_nowait()
                t = item.get("type")
                if t == "text":
                    self._append_chat(item["username"], item["content"],
                                      item["ts"], is_me=False)
                elif t == "presence":
                    self._update_members(item["members"])
                    self._sync_video_panels(item["members"])
                elif t in ("video_participant", "video_remote"):
                    sender_id = item.get("sender_id", "")
                    if sender_id and sender_id != self.client_id:
                        username = self._participant_names.get(sender_id) or sender_id[:8]
                        panel = self._ensure_video_panel(sender_id, username)
                        panel["name"].config(text=username)
                        panel["video"].show_frame(item["jpeg"])
                        self._update_video_status()
                elif t == "video_self":
                    if self.camera_on:
                        self._video_self.show_camera_frame(item["frame"])
                elif t == "status":
                    self._status_lbl.config(text=item["msg"],
                                            fg=item.get("color", C_GREEN))
                elif t == "reconnecting":
                    self._status_lbl.config(text="⚠ Reconectando...",
                                            fg=C_ACCENT)
                    self._clear_video_panels()
                    self._append_sys("Broker desconectado. Reconectando...")
                    self._start_reconnect_flow()
                elif t == "reconnected":
                    self._status_lbl.config(text="● Conectado", fg=C_GREEN)
                    self._append_sys("Reconectado com sucesso!")
        except Exception:
            pass
        if not self._stop.is_set() or not self._gui_q.empty():
            self.root.after(33, self._poll_gui_queue)

    def _append_chat(self, username: str, content: str, ts: float,
                     is_me: bool = False):
        if not hasattr(self, "_chat_area"):
            return
        t = self._chat_area
        t.config(state="normal")
        hh = time.strftime("%H:%M:%S", time.localtime(ts))
        t.insert("end", f"[{hh}] ", "ts")
        tag = "me" if is_me else "other"
        t.insert("end", f"{username}: ", tag)
        t.insert("end", content + "\n", "")
        t.config(state="disabled")
        t.see("end")

    def _append_sys(self, msg: str):
        if not hasattr(self, "_chat_area"):
            return
        t = self._chat_area
        t.config(state="normal")
        t.insert("end", f"── {msg}\n", "sys")
        t.config(state="disabled")
        t.see("end")

    def _update_members(self, members: dict):
        if not hasattr(self, "_members_list"):
            return
        self._members_list.delete(0, "end")
        for cid, uname in members.items():
            marker = " (você)" if cid == self.client_id else ""
            self._members_list.insert("end", f"  ● {uname}{marker}")

    def _ensure_video_panel(self, sender_id: str, username: str):
        panel = self._video_panels.get(sender_id)
        if panel:
            return panel

        container = tk.Frame(
            self._video_gallery,
            bg=C_PANEL,
            highlightbackground=C_BORDER,
            highlightthickness=1,
        )
        name_label = tk.Label(container, text=username, font=FONT_SMALL,
                               fg=C_TEXT, bg=C_PANEL)
        name_label.pack(anchor="w", padx=4, pady=(4, 0))

        video_panel = VideoPanel(container, self.REM_TILE_W, self.REM_TILE_H)
        video_panel.pack(padx=4, pady=4)

        panel = {"container": container, "name": name_label, "video": video_panel}
        self._video_panels[sender_id] = panel
        self._reflow_video_panels()
        return panel

    def _clear_video_panels(self):
        for panel in self._video_panels.values():
            try:
                panel["container"].destroy()
            except Exception:
                pass
        self._video_panels.clear()
        self._update_video_status()

    def _sync_video_panels(self, members: dict):
        self._participant_names = {
            cid: uname for cid, uname in members.items() if cid != self.client_id
        }
        active_ids = set(self._participant_names)

        for sender_id in list(self._video_panels):
            if sender_id not in active_ids:
                try:
                    self._video_panels[sender_id]["container"].destroy()
                except Exception:
                    pass
                self._video_panels.pop(sender_id, None)

        for sender_id, username in self._participant_names.items():
            panel = self._ensure_video_panel(sender_id, username)
            panel["name"].config(text=username)

        self._reflow_video_panels()
        self._update_video_status()

    def _reflow_video_panels(self):
        if not hasattr(self, "_video_gallery"):
            return
        panels = sorted(
            self._video_panels.items(),
            key=lambda item: self._participant_names.get(item[0], item[0]).lower(),
        )
        for widget in self._video_gallery.winfo_children():
            widget.grid_forget()

        for index, (_, panel) in enumerate(panels):
            row = index // self.REM_TILE_COLS
            col = index % self.REM_TILE_COLS
            panel["container"].grid(row=row, column=col, padx=4, pady=4, sticky="nsew")

        for col in range(self.REM_TILE_COLS):
            self._video_gallery.grid_columnconfigure(col, weight=1)
        for row in range((len(panels) + self.REM_TILE_COLS - 1) // self.REM_TILE_COLS):
            self._video_gallery.grid_rowconfigure(row, weight=1)

    def _update_video_status(self):
        if hasattr(self, "_quality_lbl"):
            count = len(self._video_panels)
            self._quality_lbl.config(text=f"Vídeos: {count}")

    def _set_status(self, msg: str, color: str = C_GREEN):
        if hasattr(self, "_status_lbl"):
            self._status_lbl.config(text=msg, fg=color)

    def _cleanup_audio(self):
        """Encerra streams de áudio."""
        if self.audio_stream:
            try:
                self.audio_stream.stop_stream()
                self.audio_stream.close()
            except Exception:
                pass
            self.audio_stream = None
        if self.p:
            try:
                self.p.terminate()
            except Exception:
                pass
            self.p = None

    def _start_reconnect_flow(self):
        with self._reconnect_lock:
            if self._reconnect_in_progress or self._stop.is_set():
                return
            self._reconnect_in_progress = True
        threading.Thread(target=self._reconnect_worker, daemon=True).start()

    def _reconnect_worker(self):
        try:
            old_session = self._network_session
            if old_session:
                old_session.stop()

            self._cleanup_audio()

            if not self.username or not self.room:
                return

            self._session_stop = threading.Event()
            new_session = GUIClientSession(self.cfg, self.client_id,
                                           self._gui_q, self._session_stop)
            new_session.set_credentials(self.username, self.room)
            new_session.set_camera_enabled(self.camera_on)

            for i in range(15):
                if self._stop.is_set():
                    return
                if new_session.discover_broker(max_retries=1, delay=0):
                    self.broker = new_session.broker
                    if new_session.start(self.recv_queue):
                        self._network_session = new_session
                        self._start_audio_stream()
                        self.root.after(0, lambda: self._status_lbl.config(
                            text="● Conectado", fg=C_GREEN))
                        self.root.after(0, lambda: self._append_sys(
                            "Conectado ao novo broker."))
                        self.root.after(0, self._update_video_status)
                        return
                self.root.after(0, lambda i=i: self._status_lbl.config(
                    text=f"⚠ Reconectando... ({i+1}/15)", fg=C_ACCENT))
                time.sleep(1)

            self.root.after(0, lambda: self._status_lbl.config(
                text="⚠ Falha ao reconectar", fg=C_ACCENT))
            self.root.after(0, lambda: self._append_sys(
                "Não foi possível transferir para outro broker."))
        finally:
            with self._reconnect_lock:
                self._reconnect_in_progress = False

    # ------------------------------------------------------------------
    # Gerenciamento de rede via GUIClientSession
    # ------------------------------------------------------------------

    def _start_network(self):
        """Inicia conexão de rede e streams de áudio."""
        self._network_session.set_credentials(self.username, self.room)
        self._network_session.set_camera_enabled(self.camera_on)
        
        # Inicia stream de áudio com callbacks
        self._start_audio_stream()
        
        # Inicia as threads de rede
        if not self._network_session.start(self.recv_queue):
            messagebox.showerror("Erro", "Falha ao conectar ao broker")
            return False
        
        return True

    def _start_audio_stream(self):
        """Inicializa stream de áudio bidirecional com callbacks."""
        if not _AUDIO_OK or sys.platform == "win32":
            return
        
        cfg = self.cfg["client"]["audio"]
        try:
            self.p = pyaudio.PyAudio()
        except Exception as e:
            print(f"[Audio] Failed to initialize PyAudio: {e}", file=sys.stderr)
            return
        
        try:
            self.audio_stream = self.p.open(
                format=pyaudio.paInt16,
                channels=cfg["channels"],
                rate=cfg["rate"],
                input=True,
                output=True,
                frames_per_buffer=cfg["chunk"],
                stream_callback=self._audio_callback
            )
            self.audio_stream.start_stream()
        except Exception as e:
            print(f"[Audio] Failed to open stream: {e}", file=sys.stderr)
            if self.p:
                self.p.terminate()
                self.p = None
            return

    def _audio_callback(self, in_data, frame_count, time_info, status):
        """
        Callback de áudio bidirecional: captura entrada de microfone, envia para rede;
        recebe áudio da rede e retorna para saída.
        """
        # Enviar áudio capturado se não mutado
        if not self.muted and self._network_session:
            self._network_session.send_audio(in_data, self.muted)
        
        # Retornar áudio para saída (buffered ou silêncio)
        out_data = b''
        if self.speaker_on:
            try:
                out_data = self.recv_queue.get_nowait()
            except queue.Empty:
                out_data = b'\x00' * len(in_data)
        else:
            # Drain the queue while speaker is off to avoid latency when turning back on
            try:
                while True:
                    self.recv_queue.get_nowait()
            except queue.Empty:
                pass
            out_data = b'\x00' * len(in_data)
        
        return (out_data, pyaudio.paContinue)


# ---------------------------------------------------------------------------
# Ponto de entrada
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not _PIL_OK:
        print("Instale Pillow para exibir vídeo:  pip install Pillow")
    ConferenceApp()
