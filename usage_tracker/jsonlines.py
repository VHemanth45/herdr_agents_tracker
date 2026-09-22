"""A short-lived CLI that speaks JSON lines on stdin/stdout (`codex app-server`, `claude -p`)."""

import contextlib
import json
import queue
import subprocess
import threading
import time

from .model import ProviderError


class JsonLines:
    """Use as a context manager; every read shares one deadline, and the process is always stopped."""

    def __init__(self, name, cmd, env, timeout, cwd=None):
        self.name, self.deadline = name, time.monotonic() + timeout
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, bufsize=1, env=env, cwd=cwd)
        self.lines = queue.Queue()
        self.reader = threading.Thread(target=self._pump, daemon=True)
        self.reader.start()

    def _pump(self):
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def send(self, message):
        try:
            self.proc.stdin.write(json.dumps(message) + "\n")
            self.proc.stdin.flush()
        except OSError as exc:  # includes BrokenPipeError when the process already exited
            raise ProviderError("error", f"{self.name}: {exc}") from None

    def read(self):
        """The next JSON object; raises ProviderError when the deadline passes or the process exits."""
        while True:
            try:
                line = self.lines.get(timeout=max(0.05, self.deadline - time.monotonic()))
            except queue.Empty:
                raise ProviderError("error", f"{self.name} timed out") from None
            if line is None:
                raise ProviderError("error", f"{self.name} exited before answering")
            if time.monotonic() > self.deadline:
                raise ProviderError("error", f"{self.name} timed out")
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict):
                return message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        with contextlib.suppress(OSError):
            self.proc.stdin.close()
        self.proc.terminate()
        try:
            self.proc.wait(3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.reader.join(1)  # the pipe reaches EOF once the process is gone
        self.proc.stdout.close()
