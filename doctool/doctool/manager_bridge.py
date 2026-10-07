"""Связь с расширением Chrome «doctool — manager» по WebSocket (ws://127.0.0.1:<порт>/ext).

Расширение само подключается к doctool, когда Chrome запущен. doctool отправляет ему команды
(lookup — данные домена из manager) и ждёт ответ. Расширение работает от имени оператора, с его входом
в manager, и только читает страницы. doctool логины не хранит и к manager сам не обращается.

Протокол (JSON):
  расширение → doctool: {"type": "hello", "version": "0.3.0"}, {"type": "ping"},
                        {"type": "progress", "id": …, "step": …, "message": …},
                        {"type": "result", "id": …, "ok": true, "data": {…}} | {"ok": false, "error": {code, message}}
  doctool → расширение: {"type": "request", "id": …, "cmd": "lookup", "params": {"domain": …, "ascii": …}},
                        {"type": "welcome"}, {"type": "pong"}
"""
from __future__ import annotations

import asyncio
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutTimeout
from dataclasses import dataclass, field
from datetime import datetime

from .domains import ManagerUnavailable, lookup_domains
from .whois import lookup as whois_lookup


class ExtensionBridge:
    def __init__(self, allowed_origins: tuple[str, ...] = ("chrome-extension://",)):
        self.allowed_origins = allowed_origins
        self.ws = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.info: dict = {}
        self.connected_at: datetime | None = None
        self.last_seen: datetime | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._progress: dict[str, callable] = {}

    # ---------------------------------------------------------------- состояние
    def connected(self) -> bool:
        return self.ws is not None

    def status(self) -> dict:
        return {"connected": self.connected(), "version": self.info.get("version", ""),
                "connected_at": self.connected_at.isoformat(timespec="seconds") if self.connected_at else None,
                "last_seen": self.last_seen.isoformat(timespec="seconds") if self.last_seen else None,
                "pending": len(self._pending)}

    def origin_allowed(self, origin: str | None) -> bool:
        return bool(origin) and any(origin.startswith(o) for o in self.allowed_origins)

    # ---------------------------------------------------------------- WebSocket (вызывается из web.py)
    async def serve(self, ws) -> None:
        """Обработчик WebSocket-соединения расширения (FastAPI WebSocket)."""
        origin = ws.headers.get("origin")
        if not self.origin_allowed(origin):
            await ws.close(code=4403)
            return
        await ws.accept()
        old = self.ws
        self.ws, self.loop = ws, asyncio.get_running_loop()
        self.connected_at = self.last_seen = datetime.now()
        if old is not None:
            try:
                await old.close(code=4000)
            except Exception:  # noqa: BLE001
                pass
        try:
            await ws.send_json({"type": "welcome"})
            while True:
                msg = await ws.receive_json()
                self.last_seen = datetime.now()
                self._dispatch(msg)
        except Exception:  # noqa: BLE001 — разрыв соединения
            pass
        finally:
            if self.ws is ws:
                self.ws = None
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(ManagerUnavailable("расширение отключилось"))
                self._pending.clear()

    def _dispatch(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "hello":
            self.info = {k: v for k, v in msg.items() if k != "type"}
        elif t == "ping":
            asyncio.ensure_future(self.ws.send_json({"type": "pong"}))
        elif t == "progress":
            cb = self._progress.get(msg.get("id"))
            if cb:
                try:
                    cb(msg)
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
        elif t == "result":
            fut = self._pending.get(msg.get("id"))
            if fut is not None and not fut.done():
                fut.set_result({k: v for k, v in msg.items() if k not in ("type", "id")})

    # ---------------------------------------------------------------- запросы
    async def request(self, cmd: str, params: dict, timeout: float = 240, on_progress=None) -> dict:
        if self.ws is None:
            raise ManagerUnavailable("Расширение «doctool — manager» не подключено: откройте Chrome с расширением")
        rid = uuid.uuid4().hex[:12]
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        if on_progress:
            self._progress[rid] = on_progress
        try:
            await self.ws.send_json({"type": "request", "id": rid, "cmd": cmd, "params": params})
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as e:
            raise TimeoutError(f"расширение не ответило за {timeout:.0f} с") from e
        finally:
            self._pending.pop(rid, None)
            self._progress.pop(rid, None)

    def request_sync(self, cmd: str, params: dict, timeout: float = 240, on_progress=None) -> dict:
        """Для фоновых потоков (проверка дела, задача загрузки): ждёт ответ расширения."""
        if self.ws is None or self.loop is None:
            raise ManagerUnavailable("Расширение «doctool — manager» не подключено: откройте Chrome с расширением")
        fut = asyncio.run_coroutine_threadsafe(self.request(cmd, params, timeout, on_progress), self.loop)
        try:
            return fut.result(timeout + 5)
        except FutTimeout as e:
            fut.cancel()
            raise TimeoutError("расширение не ответило вовремя") from e


# ------------------------------------------------------------------ задачи загрузки (кнопка в интерфейсе)

@dataclass
class ManagerTask:
    id: str
    domains: list[str]
    job_id: str | None = None
    status: str = "running"         # running | done | error
    log: list[str] = field(default_factory=list)
    items: list = field(default_factory=list)
    error: str = ""
    started: datetime = field(default_factory=datetime.now)
    cancel: threading.Event = field(default_factory=threading.Event)


class ManagerTasks:
    def __init__(self, bridge: ExtensionBridge, use_whois: bool = True):
        self.bridge = bridge
        self.use_whois = use_whois
        self.tasks: dict[str, ManagerTask] = {}
        self.pool = ThreadPoolExecutor(max_workers=1)

    def lookup(self, domains: list[str], log=print, cancel=None) -> list:
        """Синхронная загрузка (из потока проверки дела)."""
        return lookup_domains(self.bridge.request_sync, domains, log=log,
                              whois_lookup=whois_lookup if self.use_whois else None, cancel=cancel)

    def submit(self, domains: list[str], job_id: str | None = None, on_done=None) -> ManagerTask:
        t = ManagerTask(id=uuid.uuid4().hex[:12], domains=domains, job_id=job_id)
        self.tasks[t.id] = t

        def log(msg: str):
            t.log.append(f"{(datetime.now() - t.started).total_seconds():6.1f} с  {msg}")

        def run():
            try:
                t.items = self.lookup(domains, log=log, cancel=t.cancel)
                if on_done:
                    on_done(t)
                t.status = "done"
            except Exception as e:  # noqa: BLE001
                t.status, t.error = "error", str(e)
                log(f"ОШИБКА: {e}")
                traceback.print_exc()
        self.pool.submit(run)
        return t

    def get(self, task_id: str) -> ManagerTask | None:
        return self.tasks.get(task_id)
