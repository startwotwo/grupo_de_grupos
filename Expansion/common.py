"""
Shared constants between discovery, broker and clients.
"""
 
# ----- Discovery -----
DISCOVERY_PORT = 5570
 
# Commands of the discovery REQ/REP protocol
CMD_REGISTER  = "REGISTER"   # broker -> discovery: "hello, I'm here"
CMD_HEARTBEAT = "HEARTBEAT"  # broker -> discovery: "still alive"
CMD_LIST      = "LIST"       # client -> discovery: "give me the list of brokers"
CMD_UNREGISTER = "UNREGISTER"  # broker -> discovery: "leaving"
 
# Time (seconds) without heartbeat until considering broker dead
BROKER_TIMEOUT = 5.0
# Interval between broker heartbeats
HEARTBEAT_INTERVAL = 1.5
 
# ----- Broker channel ports (relative to base_port) -----
# If a broker has base_port=5555, it uses:
#   video : 5555 (PUB-in) / 5556 (SUB-out)
#   audio : 5557 / 5558
#   text  : 5559 / 5560
# Next broker can use base_port=5575, etc.
def channel_ports(base_port):
    return {
        "video": (base_port,     base_port + 1),
        "audio": (base_port + 2, base_port + 3),
        "text":  (base_port + 4, base_port + 5),
        "presence": (base_port + 6, base_port + 7),
    }

# Available rooms
ROOMS = list("ABCDEFGHIJK")

# presence reanouncement interval (seconds)
PRESENCE_INTERVAL = 3.0

# ----- Mesh-Aware topic format -----
# Topic: "{channel}:{broker_origin}:{room}:{user_id}"
# Example: "video:B1:A:user1"

def make_topic(channel, broker_id, room, user_id):
    return f"{channel}:{broker_id}:{room}:{user_id}"

def parse_topic(topic_str):
    """Returns (channel, broker_origin, room, user_id) or None if invalid."""
    parts = topic_str.split(":", 3)
    if len(parts) != 4:
        return None
    return parts[0], parts[1], parts[2], parts[3]