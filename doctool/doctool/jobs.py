"""Очередь задач для интерфейсов: проверки выполняются по одной в фоновом потоке
(OCR и модель нагружают процессор/видеокарту), с ходом работы и отменой."""
from __future__ import annotations

import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import progress
from .compare import STATUS_RU
from .domains import PROBLEMS, sort_by_problem
from .service import CaseInput, CaseResult, attach_domains, case_domains, recompute, run_case, table_rows


@dataclass
class Job:
    id: str
    inp: CaseInput
    status: str = "queued"          # queued | running | done | error | cancelled
    log: list[str] = field(default_factory=list)
    result: CaseResult | None = None
    error: str = ""
    cancel: threading.Event = field(default_factory=threading.Event)
    started: datetime | None = None
    finished: datetime | None = None


class JobManager:
    def __init__(self, manager=None):
        """manager — manager_bridge.ManagerTasks (веб-интерфейс): загрузка данных Sd доменов из manager."""
        self.manager = manager
        self.jobs: dict[str, Job] = {}
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.lock = threading.Lock()

    def submit(self, inp: CaseInput, on_update=None) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], inp=inp)
        with self.lock:
            self.jobs[job.id] = job
        self.pool.submit(self._run, job, on_update)
        return job

    def _run(self, job: Job, on_update):
        if job.cancel.is_set():
            job.status = "cancelled"
            return
        job.status, job.started = "running", datetime.now()

        def sink(msg: str):
            job.log.append(f"{(datetime.now() - job.started).total_seconds():6.1f} с  {msg}")
            if on_update:
                on_update(job)

        progress.set_sink(sink, job.cancel)
        try:
            job.result = run_case(job.inp)
            if job.inp.manager_autoload:
                self._autoload(job)
            job.status = "done"
        except progress.Cancelled:
            job.status = "cancelled"
            job.log.append("Остановлено пользователем")
        except Exception as e:  # noqa: BLE001
            job.status, job.error = "error", f"{e}"
            job.log.append("ОШИБКА: " + "".join(traceback.format_exception_only(type(e), e)).strip())
            traceback.print_exc()
        finally:
            progress.set_sink(None, None)
            job.finished = datetime.now()
            if on_update:
                on_update(job)

    def _autoload(self, job: Job) -> None:
        """Флажок «подгрузить данные Sd доменов из заявления»: после сверки — данные доменов из manager."""
        res = job.result
        doms = case_domains(res)
        if not doms:
            progress.log("[!] В заявлении не найдены домены — данные из manager не загружались")
            return
        if self.manager is None or not self.manager.bridge.connected():
            msg = ("Данные Sd не загружены: расширение «PySimpleManager» не подключено "
                   "(Chrome с расширением должен быть открыт). Можно загрузить позже кнопкой.")
            progress.log("[!] " + msg)
            res.notes.append(msg)
            return
        progress.log(f"Загружаю данные доменов из manager: {', '.join(doms)}")
        items = self.manager.lookup(doms, log=progress.log, cancel=job.cancel)
        attach_domains(res, items)
        progress.log(f"Данные доменов загружены. Итог: {res.decision.title}")

    def attach_domains(self, job_id: str, items: list) -> Job:
        job = self.jobs[job_id]
        progress.set_sink(lambda m: None, None)
        try:
            attach_domains(job.result, items)
        finally:
            progress.set_sink(None, None)
        return job

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def recompute(self, job_id: str, overrides: dict, confirmed: list) -> Job:
        job = self.jobs[job_id]
        progress.set_sink(lambda m: None, None)
        try:
            recompute(job.result, overrides, confirmed)
        finally:
            progress.set_sink(None, None)
        return job


def result_payload(res: CaseResult, file_url) -> dict:
    """Результат в виде JSON для интерфейса. file_url(path) → ссылка на файл из папки дела."""
    rows = table_rows(res)
    for r in rows:
        r["crop_url"] = file_url(r["crop"]) if r.get("crop") else None
        r.pop("crop", None)
    notes = list(res.notes)
    for ex in (res.app, res.pas):
        if ex is not None:
            notes += ex.notes
    return {
        "case_id": res.case_id,
        "case_type": res.case_type.get("title"),
        "decision": res.decision.to_dict(),
        "rows": rows,
        "checks": [{**c.to_dict(), "crop_url": file_url(c.crop) if c.crop else None, "crop": None}
                   for c in res.checks],
        "notes": notes,
        "page_roles": {str(k): v for k, v in res.page_roles.items()},
        "previous_passports": res.previous_passports,
        # по проблемности: свободные по WHOIS → данные не сходятся → … → в порядке (domains.PROBLEMS)
        "domains": [d.to_dict() for d in sort_by_problem(res.domains_sd)],
        "domain_groups": dict(PROBLEMS),
        "domains_from_app": case_domains(res),
        "record": res.record,
        "status_ru": STATUS_RU,
        "case_dir": str(Path(res.case_dir).resolve()),
        "report_url": file_url(str(Path(res.case_dir) / "report.html")),
        "export_url": file_url(str(Path(res.case_dir) / "export.json")),
        "regions_url": file_url(res.app.debug["regions_overlay"]) if res.app and res.app.debug.get("regions_overlay") else None,
    }
