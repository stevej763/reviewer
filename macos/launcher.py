"""Starts the Reviewer server for the Brain Dump Mac app: python3 launcher.py <port>.

Exits when the app that started it goes away, even if the app was force-quit."""

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
    uvicorn.run("app.main:create_app", factory=True, host="127.0.0.1", port=int(sys.argv[1]))
