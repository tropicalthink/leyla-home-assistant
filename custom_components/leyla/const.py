"""Constants for the Leyla integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "leyla"

# What this client tells the server about itself when it asks for a sign-in
# code. The server names the session from it ("Leyla app on Home
# Assistant"), which is what the person sees in Your devices and on a TV
# that shows who controls it. Home Assistant runs on many systems, so no
# platform is claimed; it is not a phone, tablet or TV, so "desktop".
CLIENT_DEVICE: Final = {"form_factor": "desktop", "model": "Home Assistant"}

EVENT_LIBRARY_UPDATED: Final = "leyla_library_updated"

TOPIC_REMOTE: Final = "remote"
TOPIC_CATALOG: Final = "catalog"
TOPIC_ADMIN_NOW_PLAYING: Final = "admin.now_playing"

CLOSE_SIGNED_OUT: Final = 4401
CLOSE_ACCESS_CHANGED: Final = 4403

# Reconnect backoff: the first retry after a second, doubling to a few
# minutes, so an absent server sees one request now and then, never a burst.
BACKOFF_FIRST: Final = 1.0
BACKOFF_MAX: Final = 300.0
# A socket that lived this long counts as a good connection and resets the
# backoff; one that drops at once does not, so a server that accepts and
# closes is not hammered.
STABLE_AFTER: Final = 60.0

# aiohttp sends its own ping on this interval and treats a missing pong as
# a dead link. The server pings every 20 s and answers ours.
HEARTBEAT: Final = 30.0

COMMAND_TIMEOUT: Final = 10.0

# The admin streams list: refetched shortly after the server says it
# changed, and slowly on a timer in case an event was missed.
NOW_PLAYING_DEBOUNCE: Final = 2.0
NOW_PLAYING_POLL: Final = timedelta(minutes=5)

# A sign-in is a 30-day sliding session that HTTP requests extend. The
# socket alone does not, so the hub touches the account twice a day.
KEEPALIVE: Final = timedelta(hours=12)

# A conversion is a session whose plan re-encodes the picture
# (`video_action` "transcode" in the server's now-playing rows). An HLS
# session that copies the picture is a remux, which costs next to nothing.
CONVERTING_VIDEO_ACTION: Final = "transcode"

MAX_SESSIONS_IN_ATTRIBUTES: Final = 10
