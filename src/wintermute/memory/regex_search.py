"""Isolate stdlib regex matching so pathological expressions can be terminated."""

import json
import subprocess
import sys
import threading
from types import TracebackType
from typing import Self

_WORKER = """
import json, re, sys
for request in sys.stdin:
    try:
        data = json.loads(request)
        pattern = re.compile(data['query'], re.IGNORECASE)
        matches = []
        for number, line in enumerate(data['text'].splitlines()):
            if pattern.search(line):
                matches.append(number)
                if len(matches) >= data['limit']:
                    break
        result = {'lines': matches}
    except Exception:
        result = {'error': 'Invalid regular expression'}
    print(json.dumps(result), flush=True)
"""


class RegexMatcher:
    """One disposable worker per search, with a hard timeout per document."""

    def __init__(self, query: str, *, timeout_seconds: float = 1) -> None:
        self.query = query
        self.timeout_seconds = timeout_seconds
        self.process = subprocess.Popen(
            [sys.executable, "-I", "-c", _WORKER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def matches(
        self, text: str, limit: int, *, timeout_seconds: float | None = None
    ) -> list[int]:
        assert self.process.stdin is not None and self.process.stdout is not None
        expired = threading.Event()

        def terminate() -> None:
            expired.set()
            if self.process.poll() is None:
                self.process.kill()

        budget = self.timeout_seconds
        if timeout_seconds is not None:
            budget = min(budget, timeout_seconds)
        if budget <= 0:
            raise ValueError("Regex search exceeded its time limit; simplify the expression")
        timer = threading.Timer(budget, terminate)
        timer.daemon = True
        timer.start()
        try:
            request = json.dumps({"query": self.query, "text": text, "limit": limit})
            self.process.stdin.write(request + "\n")
            self.process.stdin.flush()
            response = self.process.stdout.readline()
            if expired.is_set() or not response:
                raise ValueError("Regex search exceeded its time limit; simplify the expression")
            result: object = json.loads(response)
            if not isinstance(result, dict) or "error" in result:
                raise ValueError("Invalid regular expression")
            lines = result.get("lines")
            if not isinstance(lines, list) or not all(isinstance(item, int) for item in lines):
                raise ValueError("Invalid regex worker response")
            return lines
        except (BrokenPipeError, OSError) as exc:
            raise ValueError("Regex search worker stopped; simplify the expression") from exc
        finally:
            timer.cancel()
            timer.join()

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()
        for stream in (self.process.stdin, self.process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
