"""Run tests in workers forked from one warmed-up process instead of fresh interpreters.

pytest-xdist starts each worker as a new interpreter (execnet's popen gateway), so every
worker re-imports the whole dependency tree and pays its full resident cost. Here collection
and every import happen once, in the controller; workers are ``fork()`` children that share
that heap copy-on-write, so N workers cost roughly one worker plus what the tests allocate.
"""

from __future__ import annotations

import argparse
import contextlib
import gc
import json
import os
import selectors
import signal
import socket
import struct
import sys
import traceback
from typing import Any

import pytest
from _pytest.reports import TestReport
from _pytest.runner import runtestprotocol

_HEADER = struct.Struct("!I")


class _PeerGone(OSError):
    """The other end of a worker socket closed; an OSError so one except covers socket errors too."""


def _count(value: str) -> int:
    """Worker count, or 'auto' for one per core — the spelling xdist and pytest-parallel both take."""
    if value == "auto":
        return os.cpu_count() or 1
    try:
        count = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number or 'auto', got {value!r}") from None
    if count < 0:
        raise argparse.ArgumentTypeError("cannot be negative")
    return count


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("zygote", "forked workers that share the controller's memory")
    group.addoption(
        "--workers",
        metavar="N",
        type=_count,
        default=0,
        help="Run tests in N workers forked from this process, or 'auto' for one per core.",
    )
    group.addoption(
        "--workers-no-freeze",
        action="store_true",
        help="Skip gc.freeze() before forking (diagnostic): no saving once a worker runs a full GC.",
    )
    group.addoption(
        "--workers-max-restarts",
        metavar="N",
        type=int,
        default=-1,
        help="Replace at most N workers that die mid-run (default: one per worker; 0 disables).",
    )


@pytest.hookimpl(tryfirst=True)
def pytest_runtestloop(session: pytest.Session) -> object | None:
    config = session.config
    workers: int = config.getoption("workers") or 0
    if workers < 1 or config.option.collectonly:
        return None
    if not hasattr(os, "fork"):
        raise pytest.UsageError("--workers needs os.fork(), which this platform lacks")
    if config.getoption("numprocesses", None):
        raise pytest.UsageError("--workers and -n are alternatives; pass only one")
    if session.testsfailed and not config.option.continue_on_collection_errors:
        raise session.Failed(f"{session.testsfailed} error(s) during collection")
    if session.items:
        _Controller(session, workers).run()
    return True


# --------------------------------------------------------------------------- wire


def _send(sock: socket.socket, msg: dict[str, Any]) -> None:
    payload = json.dumps(msg).encode()
    sock.sendall(_HEADER.pack(len(payload)) + payload)


def _recv_one(sock: socket.socket) -> dict[str, Any]:
    """Block until one whole message has arrived; raise _PeerGone if the peer closed first."""

    def exact(n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise _PeerGone
            buf += chunk
        return buf

    (size,) = _HEADER.unpack(exact(_HEADER.size))
    return json.loads(exact(size))


class _Reader:
    """Reassembles length-prefixed messages from a non-blocking socket."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buf = bytearray()

    def read(self) -> list[dict[str, Any]]:
        chunk = self._sock.recv(1 << 16)
        if not chunk:
            raise _PeerGone
        self._buf += chunk
        out = []
        while len(self._buf) >= _HEADER.size:
            (size,) = _HEADER.unpack_from(self._buf)
            end = _HEADER.size + size
            if len(self._buf) < end:
                break
            out.append(json.loads(bytes(self._buf[_HEADER.size : end])))
            del self._buf[:end]
        return out


# --------------------------------------------------------------------------- controller


class _Worker:
    def __init__(self, wid: int, pid: int, sock: socket.socket) -> None:
        self.id = wid
        self.pid = pid
        self.sock = sock
        self.reader = _Reader(sock)
        self.running: int | None = None  # item index in flight, for crash attribution
        self.committed: int | None = None  # item promised as this worker's next
        self.alive = True


class _Controller:
    def __init__(self, session: pytest.Session, workers: int) -> None:
        self.session = session
        self.config = session.config
        self.items = session.items
        self.count = max(1, min(workers, len(session.items)))
        files: dict[str, list[int]] = {}
        for index, item in enumerate(session.items):
            files.setdefault(str(item.path), []).append(index)
        self.files = list(files.values())  # not yet handed to any worker
        self.mine: dict[int, list[int]] = {}  # each worker's remaining share of its current file
        self.workers: list[_Worker] = []
        restarts = self.config.getoption("workers_max_restarts")
        restarts = self.count if restarts is None or restarts < 0 else int(restarts)
        self.restarts_left: int = restarts
        self._capman: Any = None
        self._next_id = 0

    def run(self) -> None:
        capman: Any = self.config.pluginmanager.getplugin("capturemanager")
        self._capman = capman
        if capman:
            # Fork with the real stdout/stderr fds restored: a child that inherited the
            # controller's capture would write into its temp files, interleaving with
            # every other child.
            capman.suspend_global_capture(in_=True)
        if not self.config.getoption("workers_no_freeze"):
            gc.collect()
            gc.freeze()
        self._announce()
        self._flush()
        self._spawn(capman)
        try:
            self._dispatch()
        except BaseException:
            self._signal_all(signal.SIGKILL)
            raise
        finally:
            self._reap()
            if capman:
                capman.resume_global_capture()

    def _flush(self) -> None:
        """Empty the controller's buffers before forking; a child inherits them and flushes its own."""
        reporter: Any = self.config.pluginmanager.getplugin("terminalreporter")
        if reporter is not None:
            with contextlib.suppress(Exception):
                reporter._tw.flush()
        for stream in (sys.stdout, sys.stderr):
            with contextlib.suppress(OSError, ValueError):
                stream.flush()

    def _announce(self) -> None:
        reporter: Any = self.config.pluginmanager.getplugin("terminalreporter")
        if reporter is None:
            return
        frozen = (
            ""
            if self.config.getoption("workers_no_freeze")
            else f", {gc.get_freeze_count()} objects frozen"
        )
        plural = "worker" if self.count == 1 else "workers"
        reporter.write_line(f"zygote: {self.count} {plural} forked from pid {os.getpid()}{frozen}")

    def _spawn(self, capman: Any) -> None:
        for _ in range(self.count):
            self.workers.append(self._fork_one(capman))

    def _fork_one(self, capman: Any) -> _Worker:
        wid = self._next_id
        self._next_id += 1
        ours, theirs = socket.socketpair()
        pid = os.fork()
        if pid == 0:
            ours.close()
            os._exit(_worker_main(self.session, theirs, wid, capman))
        theirs.close()
        ours.setblocking(False)
        return _Worker(wid, pid, ours)

    def _dispatch(self) -> None:
        selector = selectors.DefaultSelector()
        for worker in self.workers:
            selector.register(worker.sock, selectors.EVENT_READ, worker)
        live = len(self.workers)
        while live:
            for key, _ in selector.select():
                worker = key.data
                try:
                    messages = worker.reader.read()
                except OSError:
                    selector.unregister(worker.sock)
                    live -= 1
                    self._bury(worker)
                    replacement = self._replace(worker)
                    if replacement is not None:
                        selector.register(replacement.sock, selectors.EVENT_READ, replacement)
                        live += 1
                    continue
                for message in messages:
                    self._handle(worker, message)
                if not worker.alive:
                    selector.unregister(worker.sock)
                    live -= 1
        selector.close()

    def _replace(self, dead: _Worker) -> _Worker | None:
        """Fork a fresh worker so a killed one does not strand the tests still queued."""
        if not self._pending() or self._should_stop() or self.restarts_left <= 0:
            return None
        self.restarts_left -= 1
        self._flush()
        worker = self._fork_one(self._capman)
        self.workers.append(worker)
        reporter: Any = self.config.pluginmanager.getplugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(f"zygote: replaced dead gw{dead.id} with gw{worker.id}")
        return worker

    def _handle(self, worker: _Worker, message: dict[str, Any]) -> None:
        hook = self.config.hook
        kind = message["t"]
        if kind == "idle":
            self._assign(worker)
        elif kind == "start":
            hook.pytest_runtest_logstart(nodeid=message["nodeid"], location=tuple(message["location"]))
        elif kind == "report":
            report = hook.pytest_report_from_serializable(config=self.config, data=message["data"])
            hook.pytest_runtest_logreport(report=_retuple(report))
        elif kind == "finish":
            worker.running = None
            hook.pytest_runtest_logfinish(
                nodeid=message["nodeid"], location=tuple(message["location"])
            )
        elif kind == "bye":
            worker.alive = False

    def _assign(self, worker: _Worker) -> None:
        """Hand out the next test plus the one after it, so teardown knows its nextitem.

        pytest finalizes a fixture when the following test no longer needs it, which means
        the worker must know that test before it tears down. Committing one item ahead buys
        that without giving up dynamic scheduling.
        """
        if self._should_stop():
            _send(worker.sock, {"t": "stop"})
            return
        index = worker.committed if worker.committed is not None else self._take(worker)
        if index is None:
            _send(worker.sock, {"t": "stop"})
            return
        worker.committed = None if self._should_stop() else self._take(worker)
        worker.running = index
        _send(worker.sock, {"t": "run", "i": index, "next": worker.committed})

    def _take(self, worker: _Worker) -> int | None:
        """The rest of this worker's file, else a new file, else the back half of the longest share."""
        mine = self.mine.setdefault(worker.id, [])
        if not mine and self.files:
            mine.extend(self.files.pop(0))
        if not mine:
            victim = max(self.mine.values(), key=len)
            half = len(victim) // 2
            mine.extend(victim[len(victim) - half :])
            del victim[len(victim) - half :]
        return mine.pop(0) if mine else None

    def _pending(self) -> bool:
        return bool(self.files) or any(self.mine.values())

    def _should_stop(self) -> bool:
        return bool(self.session.shouldstop or self.session.shouldfail)

    def _bury(self, worker: _Worker) -> None:
        """Report the in-flight test as failed, so an OOM-killed worker never loses one silently."""
        worker.alive = False
        status = 0
        with contextlib.suppress(ChildProcessError):
            _, status = os.waitpid(worker.pid, 0)
        orphans = self.mine.pop(worker.id, [])
        if worker.committed is not None:
            orphans.insert(0, worker.committed)
            worker.committed = None
        if orphans:
            self.files.insert(0, orphans)
        if worker.running is None:
            return
        item = self.items[worker.running]
        worker.running = None
        self.config.hook.pytest_runtest_logreport(report=_crash_report(item, worker.id, status))

    def _signal_all(self, sig: int) -> None:
        for worker in self.workers:
            with contextlib.suppress(ProcessLookupError):
                os.kill(worker.pid, sig)

    def _reap(self) -> None:
        for worker in self.workers:
            with contextlib.suppress(ChildProcessError):
                os.waitpid(worker.pid, 0)
            worker.sock.close()


def _retuple(report: Any) -> Any:
    """Undo JSON's list-for-tuple substitution; the terminal reporter asserts on a skip's tuple."""
    if isinstance(report.longrepr, list):
        report.longrepr = tuple(report.longrepr)
    if isinstance(report.location, list):
        report.location = tuple(report.location)
    return report


def _crash_report(item: pytest.Item, wid: int, status: int) -> TestReport:
    signum = status & 0x7F
    how = (
        f"killed by signal {signum} ({signal.Signals(signum).name})"
        if signum
        else f"exited with code {status >> 8}"
    )
    hint = (
        "\nIn a container SIGKILL here is almost always the memory cgroup."
        if signum == signal.SIGKILL
        else ""
    )
    return TestReport(
        nodeid=item.nodeid,
        location=item.location,
        keywords={},
        outcome="failed",
        longrepr=f"zygote worker gw{wid} {how} while running this test.{hint}",
        when="call",
    )


# --------------------------------------------------------------------------- worker


def _worker_main(session: pytest.Session, sock: socket.socket, wid: int, capman: Any) -> int:
    config = session.config
    try:
        reporter = config.pluginmanager.getplugin("terminalreporter")
        if reporter is not None:
            config.pluginmanager.unregister(reporter)
        if capman:
            capman.stop_global_capturing()
            capman.start_global_capturing()
            capman.suspend_global_capture(in_=True)
        os.environ["PYTEST_ZYGOTE_WORKER"] = f"gw{wid}"
        _worker_loop(session, sock)
        return 0
    # A worker must report why it stopped, whatever stopped it.
    except BaseException:
        traceback.print_exc(file=sys.stderr)
        return 1
    finally:
        # os._exit() skips stdio flushing, and under -s the worker's own prints are still buffered.
        for stream in (sys.stdout, sys.stderr):
            with contextlib.suppress(OSError, ValueError):
                stream.flush()
        with contextlib.suppress(OSError):
            sock.close()


def _worker_loop(session: pytest.Session, sock: socket.socket) -> None:
    config = session.config
    items = session.items
    while True:
        _send(sock, {"t": "idle"})
        try:
            message = _recv_one(sock)
        except _PeerGone:
            return
        if message["t"] == "stop":
            _send(sock, {"t": "bye"})
            return
        item = items[message["i"]]
        nextitem = items[message["next"]] if message["next"] is not None else None
        location = list(item.location)
        _send(sock, {"t": "start", "nodeid": item.nodeid, "location": location})
        # log=False keeps pytest_runtest_logreport out of the worker: the controller is the
        # only process that fires it, so terminal output and result files stay single-writer.
        for report in runtestprotocol(item, log=False, nextitem=nextitem):
            data = config.hook.pytest_report_to_serializable(config=config, report=report)
            _send(sock, {"t": "report", "data": data})
        _send(sock, {"t": "finish", "nodeid": item.nodeid, "location": location})
