"""Локальный веб-интерфейс: python -m doctool web  →  http://127.0.0.1:8765

Работает без интернета: страница и скрипты отдаются этим же сервером, внешних CDN нет.
Сюда же подключается расширение Chrome «doctool — manager» (WebSocket /ext) — через него
загружаются данные доменов (Sd, S) из manager.
"""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from urllib.parse import quote

import cv2
import numpy as np
from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from . import domain_report
from .domains import sort_by_problem, split_domains
from .integrations import push_to_system
from .jobs import JobManager, result_payload
from .manager_bridge import ExtensionBridge, ManagerTasks
from .ocr import IMAGE_EXT, OllamaVLM, downscale
from .service import APP_MODES, PAS_MODES, CaseInput
from .verdict import load_case_types

STATIC = Path(__file__).resolve().parent / "static"


def create_app(out_root: str = "results", default_model: str = "qwen3-vl:4b-instruct",
               ollama: str = "http://127.0.0.1:11434") -> FastAPI:
    app = FastAPI(title="doctool", docs_url=None, redoc_url=None)
    bridge = ExtensionBridge()
    manager = ManagerTasks(bridge)
    jobs = JobManager(manager=manager)
    app.state.bridge, app.state.manager, app.state.jobs = bridge, manager, jobs
    uploads = Path(out_root) / "_uploads"

    def file_url(job_id: str):
        def f(path: str) -> str:
            return f"/api/jobs/{job_id}/file?path={quote(str(Path(path).resolve()))}"
        return f

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/api/meta")
    def meta():
        types = [{"id": k, "title": v["title"], "enabled": v.get("enabled", True), "has_form": bool(v.get("form"))}
                 for k, v in load_case_types().items()]
        models = []
        try:
            import urllib.request
            with urllib.request.urlopen(f"{ollama}/api/tags", timeout=2) as r:
                models = [m["name"] for m in json.load(r).get("models", [])]
        except Exception:  # noqa: BLE001
            pass
        return {"case_types": types, "app_modes": APP_MODES, "pas_modes": PAS_MODES,
                "default_model": default_model, "ollama": ollama, "ollama_models": models}

    def _save(f: UploadFile | None, folder: Path) -> str | None:
        if f is None or not f.filename:
            return None
        folder.mkdir(parents=True, exist_ok=True)
        dst = folder / Path(f.filename).name
        with dst.open("wb") as out:
            shutil.copyfileobj(f.file, out)
        return str(dst)

    @app.post("/api/jobs")
    async def create_job(options: str = Form(...), application: UploadFile | None = File(None),
                         passport: UploadFile | None = File(None)):
        opt = json.loads(options)
        folder = uploads / uuid.uuid4().hex[:12]
        app_path, pas_path = _save(application, folder), _save(passport, folder)
        if not app_path and not pas_path:
            raise HTTPException(400, "Загрузите хотя бы один файл")
        roles = opt.get("page_roles")
        inp = CaseInput(
            case_type=opt.get("case_type", "admin_change_person"),
            application=app_path or (pas_path if opt.get("combined") else None),
            passport=None if opt.get("combined") else pas_path,
            combined=bool(opt.get("combined")),
            page_roles={int(k): v for k, v in roles.items()} if roles else None,
            app_mode=opt.get("app_mode", "auto"), app_handwritten=bool(opt.get("handwritten")),
            pas_mode=opt.get("pas_mode", "auto"),
            vlm_model=(opt.get("model") or default_model) if opt.get("use_vlm") else None,
            ollama=ollama,
            case_id=opt.get("case_id") or None, out_root=out_root,
            manager_autoload=bool(opt.get("manager_autoload")))
        job = jobs.submit(inp)
        return {"job_id": job.id}

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str, since: int = 0):
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Нет такой задачи")
        data = {"status": job.status, "log": job.log[since:], "log_len": len(job.log), "error": job.error,
                "combined": job.inp.combined}
        if job.status == "done" and job.result is not None:
            data["result"] = result_payload(job.result, file_url(job_id))
        return data

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        job = jobs.get(job_id)
        if job:
            job.cancel.set()
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/recompute")
    async def recompute(job_id: str, body: dict):
        job = jobs.get(job_id)
        if not job or job.result is None:
            raise HTTPException(404, "Нет результата")
        jobs.recompute(job_id, body.get("overrides") or {}, body.get("confirmed") or [])
        return result_payload(job.result, file_url(job_id))

    @app.post("/api/jobs/{job_id}/push")
    def push(job_id: str):
        job = jobs.get(job_id)
        if not job or job.result is None:
            raise HTTPException(404, "Нет результата")
        try:
            push_to_system(job.result.record)
        except NotImplementedError as e:
            return JSONResponse({"ok": False, "message": str(e)}, status_code=501)
        return {"ok": True}

    @app.get("/api/jobs/{job_id}/file")
    def get_file(job_id: str, path: str):
        job = jobs.get(job_id)
        if not job or job.result is None:
            raise HTTPException(404)
        p = Path(path).resolve()
        root = Path(job.result.case_dir).resolve()
        if root not in p.parents and p != root:
            raise HTTPException(403)
        if not p.exists():
            raise HTTPException(404)
        if p.suffix == ".json":
            return FileResponse(p, media_type="application/json", filename=p.name)
        return FileResponse(p)

    @app.get("/api/jobs/{job_id}/page/{n}")
    def page_thumb(job_id: str, n: int):
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404)
        src = Path(job.inp.application or job.inp.passport)
        if src.suffix.lower() == ".pdf":
            import pymupdf
            with pymupdf.open(src) as d:
                if n >= d.page_count:
                    raise HTTPException(404)
                return Response(d[n].get_pixmap(dpi=40).tobytes("png"), media_type="image/png")
        if src.suffix.lower() in IMAGE_EXT and n == 0:
            img = cv2.imdecode(np.fromfile(str(src), dtype=np.uint8), cv2.IMREAD_COLOR)
            return Response(cv2.imencode(".png", downscale(img, 300))[1].tobytes(), media_type="image/png")
        raise HTTPException(404)

    # ---------------------------------------------------------------- manager (расширение Chrome)
    @app.websocket("/ext")
    async def ext_socket(ws: WebSocket):
        await bridge.serve(ws)

    @app.get("/api/manager/status")
    def manager_status():
        return bridge.status()

    @app.post("/api/manager/lookup")
    def manager_lookup(body: dict = Body(...)):
        doms = body.get("domains") or []
        doms = split_domains(doms if isinstance(doms, str) else " ".join(doms))
        if not doms:
            raise HTTPException(400, "Укажите домены")
        if not bridge.connected():
            raise HTTPException(409, "Расширение «doctool — manager» не подключено: откройте Chrome с расширением "
                                     "(значок расширения покажет состояние связи)")
        job_id = body.get("job_id") or None
        job = jobs.get(job_id) if job_id else None
        if job_id and (job is None or job.result is None):
            raise HTTPException(404, "Нет результата проверки для этих доменов")
        on_done = (lambda t: jobs.attach_domains(job_id, t.items)) if job else None
        t = manager.submit(doms, job_id=job_id if job else None, on_done=on_done)
        return {"task_id": t.id, "domains": doms}

    @app.get("/api/manager/tasks/{task_id}")
    def manager_task(task_id: str, since: int = 0):
        t = manager.get(task_id)
        if not t:
            raise HTTPException(404, "Нет такой задачи")
        data = {"status": t.status, "log": t.log[since:], "log_len": len(t.log), "error": t.error,
                "domains": t.domains, "items": [i.to_dict() for i in sort_by_problem(t.items)]}
        if t.status == "done" and t.job_id:
            job = jobs.get(t.job_id)
            data["result"] = result_payload(job.result, file_url(t.job_id))
        return data

    def _report_response(html_text: str, name: str) -> Response:
        return Response(html_text, media_type="text/html; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    @app.get("/api/manager/tasks/{task_id}/report")
    def manager_task_report(task_id: str):
        """Выгрузка по доменам (HTML), загруженным кнопкой «Загрузить данные из manager»."""
        t = manager.get(task_id)
        if not t or not t.items:
            raise HTTPException(404, "Нет данных доменов")
        if t.job_id:
            return job_domains_report(t.job_id)
        return _report_response(domain_report.build([i.to_dict() for i in t.items]), domain_report.filename())

    @app.get("/api/jobs/{job_id}/domains_report")
    def job_domains_report(job_id: str):
        """Выгрузка по доменам дела: с проверками дела и сверкой Sd с заявлением/паспортом."""
        job = jobs.get(job_id)
        if not job or job.result is None or not job.result.domains_sd:
            raise HTTPException(404, "Нет данных доменов")
        res = job.result
        text = domain_report.build([i.to_dict() for i in res.domains_sd], case_id=res.case_id,
                                   case_title=res.case_type.get("title", ""), checks=res.checks)
        (Path(res.case_dir) / "domains.html").write_text(text, encoding="utf-8")
        return _report_response(text, domain_report.filename(res.case_id))

    @app.post("/api/manager/tasks/{task_id}/cancel")
    def manager_cancel(task_id: str):
        t = manager.get(task_id)
        if t:
            t.cancel.set()
        return {"ok": True}

    return app


def serve(host: str = "127.0.0.1", port: int = 8765, out_root: str = "results",
          model: str = "qwen3-vl:4b-instruct", ollama: str = "http://127.0.0.1:11434", open_browser: bool = True):
    import threading
    import webbrowser

    import uvicorn
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}"
    print(f"doctool: веб-интерфейс на {url}  (остановить — Ctrl+C)")
    if host not in ("127.0.0.1", "localhost"):
        print("[!] Интерфейс доступен другим компьютерам сети. Документы будут обрабатываться на этом компьютере.")
    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(out_root, model, ollama), host=host, port=port, log_level="warning")
