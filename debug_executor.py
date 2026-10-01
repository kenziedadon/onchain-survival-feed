import sys
import signal
import os
sys.path.insert(0, os.getcwd)

def timeout_handler(signum, frame):
    raise TimeoutError("Script timed out")

signal.signal(signal.SIGALRM, timeout_handler)
signal.alarm(10)  # 10 seconds

try:
    from executor import main
    main()
except TimeoutError as e:
    print(f"TIMEOUT: {e}")
except Exception as e:
    print(f"ERROR: {e}")
    import traceback
    traceback.print_exc()
finally:
    signal.alarm(0)
