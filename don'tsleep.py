import ctypes
import time

ES_CONTINUOUS       = 0x80000000
ES_SYSTEM_REQUIRED  = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

DURATION = 2 * 60 * 60  # 2 hours in seconds

# Tell Windows: don't sleep, keep the display on
ctypes.windll.kernel32.SetThreadExecutionState(
    ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
)
print("Keeping the laptop awake for 2 hours...")

try:
    time.sleep(DURATION)
finally:
    # Restore normal sleep behavior
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    print("Done. Normal sleep settings restored.")