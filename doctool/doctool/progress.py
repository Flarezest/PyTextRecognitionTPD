"""Ход работы и отмена.

По умолчанию сообщения идут в stderr. GUI подменяет «приёмник» для своего потока
(set_sink) и может попросить остановиться (request_cancel) — тогда ближайший вызов
log() выбросит Cancelled.
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Callable

quiet = False
_local = threading.local()


class Cancelled(Exception):
    """Обработка остановлена пользователем."""


def set_sink(fn: Callable[[str], None] | None, cancel_event: threading.Event | None = None) -> None:
    _local.sink = fn
    _local.cancel = cancel_event
    _local.t0 = time.time()


def log(msg: str) -> None:
    ev = getattr(_local, "cancel", None)
    if ev is not None and ev.is_set():
        raise Cancelled()
    sink = getattr(_local, "sink", None)
    if sink is not None:
        sink(msg)
        return
    if not quiet:
        t0 = getattr(_local, "t0", None) or _T0
        print(f"[{time.time() - t0:6.1f} с] {msg}", file=sys.stderr, flush=True)


_T0 = time.time()
