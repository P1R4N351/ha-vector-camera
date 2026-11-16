"""Constants for the Vector (wire-pod) camera integration. Authored-by: CPCS."""

DOMAIN = "vector_wirepod"
PLATFORMS = ["camera", "switch"]

CONF_WIREPOD_URL = "wirepod_url"
CONF_SERIAL = "serial"
CONF_NAME = "name"
CONF_STILL_REFRESH_S = "still_refresh_s"

DEFAULT_WIREPOD_URL = "http://escapepod.local:8080"
DEFAULT_STILL_REFRESH_S = 300
MAX_STILL_REFRESH_S = 86400

BATTERY_POLL_S = 60
BATTERY_TIMEOUT_S = 20.0
# HA gives async_camera_image 10 s; answer well inside it.
STILL_WAIT_S = 7.0

BATTERY_LEVELS = {0: "unknown", 1: "low", 2: "nominal", 3: "full"}
