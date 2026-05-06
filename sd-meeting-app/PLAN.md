# PLAN - Distributed Meeting App

## Project Overview

A distributed video conferencing application supporting video, audio, and text communication across multiple rooms managed by a broker mesh. Built in Python 3 with ZeroMQ for async message routing and Python threading for media capture/playback.

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────┐
│                      Registry                        │  ← Service Discovery (REQ/REP)
│                  (porta 5500)                        │
└──────────────┬───────────────────────────────────────┘
               │ register / heartbeat / query_room
       ┌───────┴────────┐
       ▼                ▼
┌─────────────┐   ┌─────────────┐       Inter-broker
│  Broker-0   │◄─►│  Broker-1   │  ←  ROUTER/DEALER + heartbeat PUB/SUB
│  Salas A-D  │   │  Salas E-H  │
└──────┬──────┘   └──────┬──────┘
       │ PULL/PUB        │ PULL/PUB
  ┌────┴────┐       ┌────┴────┐
  │  alice  │       │  carol  │   ← PUSH/SUB/DEALER (texto, áudio, vídeo)
  │   bob   │       │         │
  └─────────┘       └─────────┘
```

### Key Components

- **Registry**: Service discovery; maintains broker registry and room-to-broker mappings; reassigns orphaned rooms to live brokers
- **Brokers**: Distributed mesh; each manages rooms and client connections; forwards media within rooms; exchanges heartbeats and room transfers with peers
- **Clients**: Connect via Registry discovery; send/receive media via Broker's PUB/SUB; send control messages via DEALER/ROUTER
- **Heartbeat System**: Client→Broker (liveness), Broker↔Registry (registration), Broker↔Broker (peer health and room reassignment)

### ZeroMQ Patterns Used

| Padrão | Sockets | Propósito |
|---|---|---|
| **PUSH → PULL** | Cliente → Broker | Envio de mídia (texto, áudio, vídeo) com backpressure |
| **PUB → SUB** | Broker → Clientes | Distribuição por sala (topic filter `"text:A"`, `"audio:F"`) |
| **DEALER ↔ ROUTER** | Cliente ↔ Broker | Controle: login, ACK de texto, presença, heartbeat, leave |
| **DEALER → ROUTER** | Broker → Broker | Relay inter-broker (mensagens para salas de outros brokers) |
| **PUB → SUB** | Broker → Broker | Heartbeat entre brokers (topic `"hb"`) |
| **REQ → REP** | Brokers/Clientes → Registry | Service discovery, broker registration, room reassignment |

### QoS Strategy

| Canal | Estratégia | Implementação |
|---|---|---|
| **Texto** | Confiabilidade | ACK do broker + reenvio automático (até 5 tentativas, intervalo 200 ms) |
| **Áudio** | Baixa latência | Buffer FIFO com descarte de frames antigos (drop-oldest) |
| **Vídeo** | Real-time adaptativo | Buffer com adaptação: reduz FPS (15→5) e compressão JPEG (70%→30%) sob alta carga |

---

## Implementation Details

### Threading Model

Each client session (`GUIClientSession`) spawns independent threads for concurrency:

```
Main Application Thread
├── _th_text_send()       → Enqueue outbound text
├── _th_text_recv()       → Dequeue inbound text → GUI
├── _th_audio_send()      → Read mic → Enqueue → Broker
├── _th_audio_recv()      → Dequeue → Speaker (via PyAudio callback)
├── _th_video_send()      → Read camera → Enqueue → Broker
├── _th_video_recv()      → Dequeue → GUI render (emits video_participant event)
├── _th_client_hb()       → Periodic heartbeat to broker (3s interval)
└── _th_hb_monitor()      → Detect heartbeat loss → trigger reconnect
```

**Key distinction**: `_stop` (app lifetime) vs `_session_stop` (network session lifetime) allows clean reconnects without restarting the GUI.

### Liveness & Failover

**Client Liveness**:
- `_th_client_hb()` sends periodic heartbeat (3s interval) to broker
- Broker updates `last_seen` timestamp for each member
- Broker's background thread prunes stale members (timeout: 6s)
- On stale member removal, presence is republished

**Broker Failure Detection**:
- `_th_hb_monitor()` detects consecutive heartbeat misses from broker
- On broker loss, signals `"reconnecting"` event to GUI
- GUI's `_reconnect_worker()` orchestrates session restart:
  1. Stops old `GUIClientSession`
  2. Cleans up PyAudio stream and media resources
  3. **Preserves camera state** (`camera_on` flag)
  4. Rediscovers broker via Registry
  5. Creates new `GUIClientSession` with preserved camera state
  6. Restarts audio/video media threads

**Room Transfer on Broker Death**:
- When Registry detects dead broker (via heartbeat timeout), it reassigns orphaned rooms
- Registry calls `_pick_broker_for_room()` to select live broker
- Clients in reassigned rooms are unaffected (they reconnect to new broker via Registry discovery)

### Media Lifecycle

**Camera Pause/Resume**:
- `_camera_enabled` threading.Event controls capture
- When disabled: `_th_video_send()` releases `cv2.VideoCapture` (frees hardware resource)
- When enabled: `_th_video_send()` reopens `cv2.VideoCapture`
- State persists across reconnects: new session inherits `camera_on` state via `set_camera_enabled(bool)`

**Multi-Participant Display**:
- `_video_panels: dict[sender_id → panel_dict]` maintains per-participant tiles
- `_th_video_recv()` emits `video_participant` events with `sender_id` preserved
- GUI's `_sync_video_panels(members)` creates/destroys tiles dynamically
- Gallery layout: 2 columns, scrollable, tiles reflow on join/leave

**Media Stop on Disconnect, Restart on Reconnect**:
- Broker loss triggers reconnect worker
- Worker calls `_cleanup_audio()`: stops PyAudio stream, closes backend
- Old session is fully stopped; media queues are cleared
- New session's `start()` method calls `_start_audio_stream()` to reopen PyAudio
- Video capture restarts with preserved `camera_on` state

### Recent Fixes & Enhancements

1. **Audio Queue Draining on Speaker Toggle**: `_audio_callback()` now drains recv queue when `speaker_on=False`, preventing audio accumulation
2. **Camera Pause/Resume with Resource Release**: Added `_camera_enabled` event; camera capture released when disabled, reopened when enabled
3. **Stale Member Pruning**: Client heartbeat thread + broker-side stale-member pruning removes users from presence after timeout
4. **Multi-Participant Video Grid**: Replaced single `_video_remote` with participant-keyed tile dictionary; sender_id preserved through network and GUI layers
5. **Broker Transfer/Failover**: Reconnect worker, Registry room reassignment, inter-broker messaging for seamless room transfer
6. **Media Lifecycle Management**: Media stops on disconnect, restarts only after successful reconnect; camera state preserved across sessions

---

## Broker Architecture & Operations

### Broker Registration & Discovery

**Startup sequence:**
1. Broker starts with index (0, 1, 2, ...) and optional host (default: 127.0.0.1)
2. Computes own ports using formula: `base_port + broker_index * port_stride`
3. Assigns rooms based on index: `rooms[index * rooms_per_broker : (index + 1) * rooms_per_broker]`
4. Registers with Registry via REQ/REP, providing broker_id, host, ports, rooms
5. Launches independent threads:
   - `_registry_hb_thread()`: Sends heartbeat to Registry every 2.0 seconds
   - `_discovery_thread()`: Queries Registry every 5 seconds for new peer brokers
   - Main loop: Polls all media sockets and inter-broker sockets

**Example setup (port_stride = 100):**
- Broker-0 (idx=0): Rooms A–D, text ports 5551–5552, audio 5553–5554, video 5555–5556, control 5560, inter_broker 5600, heartbeat 5700
- Broker-1 (idx=1): Rooms E–H, text ports 5651–5652, audio 5653–5654, video 5655–5656, control 5660, inter_broker 5610, heartbeat 5710
- Broker-2 (idx=2): Rooms I–K, text ports 5751–5752, audio 5753–5754, video 5755–5756, control 5760, inter_broker 5620, heartbeat 5720

### Inter-Broker Mesh

**Peer discovery:**
- `_discovery_thread()` queries Registry for list of active brokers
- New peers are enqueued via `_new_peers_q`
- Main loop dequeues and connects via `_connect_peer()`:
  - Creates DEALER socket pointing to peer's inter_broker port
  - Subscribes to peer's heartbeat PUB on heartbeat port
  - Stores peer info (broker_id, host, ports, last_hb timestamp)

**Message relay:**
- `_forward_to_peer()` sends media to peers that own the target room
- Uses DEALER→ROUTER pattern (base64-encoded payload, relay metadata with hop counter)
- Single-hop only (hop=1 → dropped, prevents loops)
- Returns success/failure to allow fallback to local broadcast

**Heartbeat between peers:**
- Each broker publishes `{"broker_id": self.broker_id}` on heartbeat PUB port every 2s
- Peer subscribers detect death after 6s without heartbeat (implicit via ENODEV on socket poll)
- Dead peer is removed from `self.peers` dict on next main loop iteration

### Room & Member Management

**RoomManager (thread-safe):**
- Maintains `{room: {client_id: {username, last_seen}}}` with threading.Lock
- `join(room, client_id, username)`: Adds client, sets last_seen to now
- `leave(room, client_id)`: Removes client from room
- `heartbeat(room, client_id)`: Updates last_seen timestamp
- `prune_stale(timeout)`: Returns list of rooms that lost members; removes all with last_seen > timeout

**Presence broadcast:**
- On login/leave/heartbeat timeout, broker publishes presence message to `text:{room}` topic
- Payload: `{"type": "presence", "room": room, "members": {client_id: username, ...}}`
- All clients subscribed to `text:{room}` receive and render in UI

### Message Handlers (in main loop via poller)

| Handler | Triggers on | Response |
|---|---|---|
| `_on_text()` | PULL from clients | ACK via ROUTER + broadcast to room |
| `_on_audio()` | PULL from clients | Broadcast to room (no ACK) |
| `_on_video()` | PULL from clients | Broadcast to room (no ACK) |
| `_on_control()` | ROUTER (login/leave/hb) | Sends ACK, updates room members, publishes presence |
| `_on_inter_broker()` | DEALER (relay from peer) | Broadcasts to local subscribers (if hop=0) |
| `_on_heartbeat()` | SUB (from peers) | Updates peer's last_hb timestamp |

---

## Client Session Lifecycle

### GUIClientSession (client.py & client_gui.py)

**Initialization:**
1. GUI creates `GUIClientSession(cfg, client_id, gui_q, stop_event)`
2. Session stores configuration, client_id, and GUI queue
3. Creates thread-safe queues for media (text, audio, video)
4. Initializes QoS handlers: `TextQoS` (ACK + retry), `VideoQoS` (FPS/quality adaptation)

**Broker discovery:**
1. CLI calls `discover_broker(max_retries=15, delay=1.0)` (GUI calls similarly in background)
2. Creates temporary REQ socket to Registry
3. Sends: `{"type": "query_room", "room": room_name}`
4. Registry responds: `{"status": "ok", "broker": {broker_id, host, ports, rooms}}`
5. Stores broker info; returns True on success

**Media sockets:**
- PUSH (text): Sends text to `broker.pull_port`
- PUSH (audio): Sends audio to `broker.pull_port`
- PUSH (video): Sends video to `broker.pull_port`
- SUB (text): Subscribes to `text:{room}`
- SUB (audio): Subscribes to `audio:{room}`
- SUB (video): Subscribes to `video:{room}`
- DEALER (control): Sends login/leave/heartbeat to `broker.control_port`; receives ACK

**Thread model:**
```
GUIClientSession.start(gui_q) spawns:
├── _th_text_send()       → TextQoS.get_next() → PUSH
├── _th_text_recv()       → SUB → TextQoS.ack() → gui_q.put("text_msg" event)
├── _th_audio_send()      → mic (PyAudio) → PUSH
├── _th_audio_recv()      → SUB → gui_q.put("audio_frame" event)
├── _th_video_send()      → camera (OpenCV) → VideoQoS.encode() → PUSH
├── _th_video_recv()      → SUB → gui_q.put("video_participant" event, {sender_id, frame})
├── _th_client_hb()       → DEALER send {"type": "hb", "sender_id": self.client_id} every 3s
└── _th_hb_monitor()      → Subscribe to broker heartbeat; detect loss → gui_q.put("reconnecting" event)
```

**Session stop vs app stop:**
- `_session_stop` event: Used by threads within GUIClientSession; set during reconnect
- `_stop` event: Used by ConferenceApp; set only on app close
- Reconnect sets `_session_stop`, waits for threads to exit, creates fresh GUIClientSession, clears `_session_stop`, restarts threads

### Reconnection Flow (ConferenceApp._reconnect_worker)

**Trigger:** `_th_hb_monitor()` detects broker heartbeat loss (no message for 6+ seconds)

**Sequence:**
1. Signal `"reconnecting"` event to GUI
2. Set `_session_stop` event → all threads check this and exit
3. Call `_cleanup_audio()`: Stops PyAudio stream, closes context
4. Save current camera state: `camera_state = self.camera_on`
5. Destroy old GUIClientSession
6. Call `discover_broker()` with retry loop (discovery thread re-queries Registry)
7. Create new GUIClientSession with same credentials
8. Call `set_camera_enabled(camera_state)` to restore camera state
9. Call `start()` → spawns fresh media threads
10. Call `_start_audio_stream()` → reopens PyAudio and creates callback
11. Clear `_session_stop` → signals threads to run normally

**Key invariant:** During reconnect, GUI remains responsive (separate `_session_stop` from `_stop`). All old sockets are closed before new ones created.

---

## Configuration & Runtime Parameters

**config.yaml — Actual values:**

```yaml
broker:
  # Port assignment formula: base_port + broker_index * port_stride
  text:
    pub_port: 5551      # broker PUB (clients SUB on text:<room>)
    pull_port: 5552     # clients PUSH media
  audio:
    pub_port: 5553
    pull_port: 5554
  video:
    pub_port: 5555
    pull_port: 5556
  control_port: 5560    # ROUTER (clients DEALER)
  inter_broker_base_port: 5600   # DEALER relay to peers
  heartbeat_base_port: 5700      # PUB/SUB heartbeat between brokers
  port_stride: 100      # Broker N uses base + N * stride

registry:
  host: 127.0.0.1
  port: 5500
  heartbeat_timeout: 5.0   # Broker removed if no heartbeat within 5s

cluster:
  heartbeat_interval: 2.0  # Brokers send HB to registry every 2s
  heartbeat_timeout: 5.0   # Peers considered dead after 5s without heartbeat
  all_rooms: [A, B, C, D, E, F, G, H, I, J, K]
  rooms_per_broker: 4

client:
  audio:
    rate: 44100
    channels: 1
    chunk: 1024
  video:
    device_index: 0
    frame_width: 640
    frame_height: 480

qos:
  text:
    max_retry: 5
    retry_interval: 0.2    # Recheck unsent messages every 0.2s
    ack_timeout: 2.0       # Timeout for ACK from broker
  audio:
    max_queue_depth: 10    # Max frames buffered (drop-oldest if exceeded)
  video:
    max_queue_depth: 3
    base_fps: 15
    min_fps: 5
    jpeg_quality: 70
    min_jpeg_quality: 30
```

### Port Distribution (port_stride = 100)

| Component | Broker-0 | Broker-1 | Broker-2 | Formula |
|---|---|---|---|---|
| Text PUB | 5551 | 5651 | 5751 | 5551 + idx×100 |
| Text PULL | 5552 | 5652 | 5752 | 5552 + idx×100 |
| Audio PUB | 5553 | 5653 | 5753 | 5553 + idx×100 |
| Audio PULL | 5554 | 5654 | 5754 | 5554 + idx×100 |
| Video PUB | 5555 | 5655 | 5755 | 5555 + idx×100 |
| Video PULL | 5556 | 5656 | 5756 | 5556 + idx×100 |
| Control ROUTER | 5560 | 5660 | 5760 | 5560 + idx×100 |
| Inter-Broker DEALER | 5600 | 5610 | 5620 | 5600 + idx×10 |
| Heartbeat PUB | 5700 | 5710 | 5720 | 5700 + idx×10 |
| Registry REQ/REP | 5500 (fixed) |

---

## Message Protocol

### Control Messages (Client ↔ Broker via ROUTER/DEALER)

```json
// CLIENT → BROKER (via DEALER)
{"type": "login", "username": "alice", "room": "A"}
← BROKER (ACK via ROUTER): {"type": "login_ack", "room": "A", "members": {...}, "broker_id": "..."}

{"type": "leave", "room": "A"}
← ACK: {"type": "leave_ack", "room": "A"}

{"type": "hb", "sender_id": "client-uuid", "room": "A"}
← (no ACK; broker just updates last_seen)
```

### Media Messages (Multipart)

**Text/Audio/Video structure (PUSH):**
```
Frame 0: {"v": 1, "type": "text|audio|video", "room": "A", "sender_id": "...", "seq": N, "msg_id": "...", ...}
Frame 1+: [payload_frames...]
```

**Text (with ACK & retry):**
- Sender: Enqueues via `TextQoS.send()`, which tracks msg_id and retries on timeout
- Broker: Receives, sends ACK to client_id (via ROUTER identity)
- Client: `TextQoS.ack(msg_id)` removes from pending on ACK receipt
- Max 5 retries, 0.2s interval

**Audio/Video (no ACK):**
- Sender: `VideoQoS.should_send()` gates frames based on FPS; encodes JPEG
- Broker: Broadcasts to subscribers immediately (no ACK)
- Dropped frames tolerated in real-time

### Presence Messages (Broker → Clients via PUB/SUB)

```json
Topic: "text:A"
{"v": 1, "type": "presence", "room": "A", "members": {"client-id-1": "alice", "client-id-2": "bob", ...}}
```

### Inter-Broker Relay (DEALER→ROUTER)

```json
// Relay header (Frame 1):
{
  "v": 1,
  "type": "relay",
  "channel": "text|audio|video",
  "room": "F",
  "hop": 1
}
// Frames 2+: base64-encoded payload (to avoid ZMQ multipart issues)
```

### Registry Messages (REQ/REP)

```json
CLIENT → REGISTRY:
{"type": "query_room", "room": "A"}
← {"status": "ok", "broker": {"broker_id": "...", "host": "127.0.0.1", "ports": {...}, "rooms": ["A", "B", "C", "D"]}}

BROKER → REGISTRY:
{"type": "register", "broker_id": "...", "host": "127.0.0.1", "ports": {...}, "rooms": ["A", "B", "C", "D"]}
← {"status": "ok"}

{"type": "heartbeat", "broker_id": "..."}
← {"status": "ok"}

{"type": "list_brokers"}
← {"status": "ok", "brokers": [{...}, {...}, ...]}
```

---

## Stress Test & Demo (run_demo.py)

**Scenario:**
1. Start Registry
2. Start Broker-0 (rooms A–D) and Broker-1 (rooms E–H)
3. Connect alice, bob to room A (on Broker-0); carol to room F (on Broker-1)
4. Exchange text messages (demonstrates ACK + retry)
5. **Simulate Broker-0 failure** (kill -9)
6. Wait for alice & bob to detect via heartbeat timeout (~6s)
7. Start Broker-2 (rooms A–D, different instance)
8. Alice & bob reconnect automatically (Registry reassigns rooms to Broker-2)
9. Exchange messages again post-reconnect
10. Verify carol (Broker-1) remained unaffected

**Validates:**
- Service discovery (Registry)
- Broker mesh and peer discovery
- Heartbeat-based liveness detection
- Automatic reconnection with state preservation
- Room reassignment on broker death
- Message continuity post-reconnect



---

## File Structure

```
sd-meeting-app/
├── registry.py           ← Service Discovery (REQ/REP, room reassignment)
├── broker.py             ← Distributed broker (rooms, QoS, inter-broker, heartbeat)
├── client.py             ← CLI client with resilient reconnect
├── client_gui.py         ← Tkinter GUI client with multi-participant video grid
├── run_demo.py           ← Automated demo: broker failover + reconnect
├── config.yaml           ← Ports, QoS, rooms, heartbeat intervals
├── requirements.txt      ← Python dependencies
├── run_registry.sh       ← Launch registry
├── run_broker.sh         ← Launch broker (accepts index: 0, 1, 2)
├── run_client.sh         ← Launch CLI client
├── run_demo.sh           ← Launch demo
├── README.md             ← Quick start, team, installation, run instructions
└── PLAN.md               ← This file: architecture, implementation, design decisions
```

## Dependencies

- **Python 3.11+**
- **pyzmq**: ZeroMQ bindings for async messaging
- **pyaudio**: Audio capture/playback
- **opencv-python**: Video capture
- **Pillow (PIL)**: Image rendering for video tiles
- **numpy**: Numerical operations
- **pyyaml**: Configuration loading
