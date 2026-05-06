# Technical Document: Distributed Video Conferencing System

## 1. Architecture Overview
The system employs a decentralized, loosely-coupled architecture with three primary microservice components:

1. **Registry (`registry.py`)**: Acts as a simple Service Discovery mechanism. It keeps track of active brokers and their current load.
2. **Broker Cluster (`broker.py`)**: N brokers handle communication routing. Brokers register with the Registry and propagate messages (Audio, Video, Text) across the cluster so clients on different brokers can share the same "Room".
3. **Client (`client.py`)**: The endpoint application implementing PyAudio and OpenCV for media capture. It includes multithreading to handle media streams and text inputs synchronously.

## 2. Session, Groups and Presence Control
- **Identification & Sessions**: Each client specifies a `--user` handle upon execution. The login action explicitly registers this session on the memory map of the assigned broker. 
- **Groups (Rooms)**: Inter-client scopes are managed through `--room` flags. The physical ZeroMQ `PUB/SUB` bindings filter topics precisely by room strings (`"ROOM_A TEXT ..."`).
- **Presence Control**: The implementation keeps a constant mapping of sessions inside the broker. Clients have an explicit Request/Reply functionality via the text prompt (`/online`), triggering an `ONLINE` network action. The broker intercepts this payload and dynamically queries its internal user mapping to return the active session list for that specific group.

## 3. ZeroMQ Patterns Explained
To maximize performance and handle heterogeneous media demands, different ZeroMQ communication patterns were leveraged:

- **Service Discovery (`REQ/REP`)**: The client and brokers use classic Request-Reply to ask the registry for coordinates or send heartbeats.
- **Media Streaming (`PUB/SUB`)**: Broadcast data like Video frames and Audio chunks use Publisher/Subscriber. Drops are acceptable under heavy load. The client publishes to the broker, and the broker publishes to all subscribed clients in the cluster.
- **Reliable Text Messaging (`DEALER/ROUTER`)**: For text and presence commands, reliable delivery is required. The client uses an asynchronous `DEALER` socket mapped to the broker's `ROUTER` socket, which attaches client ID headers implicitly. This enables custom message Application-Level Acknowledgements (ACK).

## 4. Fault Tolerance Strategy
### Broker Failure Detection
The Service Discovery Registry receives a heartbeat every 3 seconds from active brokers. If a broker fails to send a heartbeat within 10 seconds, it is swept and removed from the active pool.

### Client Seamless Failover
The client sends text messages holding an internal `msg_id`. When it fires a text message via the `DEALER` socket, it waits up to 2000 milliseconds for a JSON-ACK containing the matched `msg_id`. 
If it fails 3 times sequentially, the client assumes the broker is dead:
1. Closes existing ZeroMQ sockets.
2. Connects to the Registry `REQ` socket to fetch a valid, alternate broker.
3. Re-establishes the `DEALER`, `PUB`, and `SUB` connections dynamically with zero application restart.

## 5. Quality of Service (QoS)
- **Text & Commands**: Guaranteed delivery via Application level ACKs and re-transmission logic.
- **Audio**: Best effort delivery via `PUB/SUB`. Low latency is prioritized over absolute reliability. PyAudio buffers naturally deal with minor jitter.
- **Video**: `PUB/SUB`. Reduced capture frequency statically blocks sender pipeline (approx. 10 FPS) to avoid congesting the Network Buffer. Resolution scaled down aggressively to 320x240 with high JPEG compression.
