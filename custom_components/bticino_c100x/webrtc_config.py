"""Shared WebRTC connectivity configuration."""

# Keep two independent STUN providers so a transient DNS or UDP failure does not
# make remote camera viewing depend on host-only ICE candidates.
STUN_URLS = (
    "stun:stun.cloudflare.com:3478",
    "stun:stun.cloudflare.com:53",
    "stun:stun.linphone.org:3478",
)

# aiortc supports only one STUN server even when RTCIceServer contains a list.
# Use the same service as the vendor Classe 100X app for the VPS-side mapping.
SERVER_STUN_URL = "stun:stun.linphone.org:3478"


def turn_servers_from_cloudflare(payload: dict) -> list[dict]:
    """Keep only TURN entries with credentials; drop browser-blocked port 53."""
    servers = []
    for server in payload.get("iceServers") or []:
        urls = server.get("urls") or []
        if isinstance(urls, str):
            urls = [urls]
        urls = [
            url
            for url in urls
            if url.startswith(("turn:", "turns:")) and ":53?" not in url and not url.endswith(":53")
        ]
        if urls and server.get("username") and server.get("credential"):
            servers.append(
                {
                    "urls": urls,
                    "username": server["username"],
                    "credential": server["credential"],
                }
            )
    return servers


def aiortc_turn_entry(ice_servers) -> dict | None:
    """Pick the first credentialed TURN server from Home Assistant's registry.

    aiortc uses only the first turn: URL it sees, so put UDP before TCP/TLS.
    """
    for server in ice_servers or []:
        urls = getattr(server, "urls", None)
        if isinstance(urls, str):
            urls = [urls]
        turn = [url for url in urls or [] if url.startswith(("turn:", "turns:"))]
        username = getattr(server, "username", None)
        credential = getattr(server, "credential", None)
        if turn and username and credential:
            turn.sort(key=lambda url: (not url.startswith("turn:"), "transport=udp" not in url))
            return {"urls": turn, "username": username, "credential": credential}
    return None
