"""Constants for the Dragontree Irrigation component."""

DOMAIN = "dragontree_irrigation"
STORAGE_KEY = "dragontree_irrigation"
STORAGE_VERSION = 1

# Rain modes
RAIN_MODE_NONE = "None"
RAIN_MODE_LIGHT = "Light"
RAIN_MODE_HEAVY = "Heavy"
RAIN_MODES = [RAIN_MODE_NONE, RAIN_MODE_LIGHT, RAIN_MODE_HEAVY]

# Schedule modes
SCHEDULE_MODE_OFF = "Off"
SCHEDULE_MODE_NORMAL = "Normal"
SCHEDULE_MODE_HOT = "Hot"
SCHEDULE_MODES = [SCHEDULE_MODE_OFF, SCHEDULE_MODE_NORMAL, SCHEDULE_MODE_HOT]

# Station statuses
STATUS_SCHEDULED = "scheduled"
STATUS_RUNNING = "running"
STATUS_MANUAL = "manual"
STATUS_COMPLETE = "complete"
STATUS_PAUSED = "paused"
STATUS_CANCELLED = "cancelled"

# Queue names
QUEUE_AM = "am"
QUEUE_PM = "pm"

# Days of week (0=Mon … 6=Sun)
DAYS_OF_WEEK = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Defaults
DEFAULT_MASTER_ENABLE = False
DEFAULT_RAIN_MODE = RAIN_MODE_NONE
DEFAULT_AM_START_TIME = "06:00"
DEFAULT_PM_START_TIME = "18:00"
DEFAULT_LOOKAHEAD_DAYS = 3
DEFAULT_MANUAL_DURATION = 600  # seconds
DEFAULT_WEEK_INTERVAL = 1

# OpenSprinkler integration
OPENSPRINKLER_DOMAIN = "opensprinkler"
OS_SERVICE_RUN_STATION = "run_station"
OS_SERVICE_STOP = "stop"

# HA platforms this component sets up
PLATFORMS = ["binary_sensor", "sensor", "switch", "select", "number", "time", "text"]

# Dispatcher signals
SIGNAL_STATIONS_UPDATED = f"{DOMAIN}_stations_updated"

# Services
SERVICE_START_STATION = "start_station"
SERVICE_STOP_STATION = "stop_station"
SERVICE_ADD_STATION = "add_station"
SERVICE_UPDATE_STATION = "update_station"
SERVICE_REMOVE_STATION = "remove_station"
SERVICE_REORDER_STATIONS = "reorder_stations"
SERVICE_UPDATE_SCHEDULE = "update_schedule"
SERVICE_MOVE_STATION = "move_station"
