"""Starts the Brain Dump server for the Mac app: python3 launcher.py <port>.

stdin and stdout are a private channel to the app (see app/macbridge.py); everything the
server prints goes to stderr, which the app sends to its log. Exits when the app that
started it goes away, even if the app was force-quit."""

import os
import sys
import threading
import time

import uvicorn


def exit_with_parent(parent: int):
    while os.getppid() == parent:
        time.sleep(2)
    os._exit(0)


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    threading.Thread(target=exit_with_parent, args=(os.getppid(),), daemon=True).start()

    # Keep the real stdout for messages to the app, and send anything printed to the log.
    control = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)

    from app.macbridge import MacBridge
    from app.main import create_app

    app = create_app()
    bridge = MacBridge(app, control)
    bridge.start()

    def listen():
        bridge.serve(sys.stdin)
        os._exit(0)  # stdin closed: the app has gone

    threading.Thread(target=listen, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]))
