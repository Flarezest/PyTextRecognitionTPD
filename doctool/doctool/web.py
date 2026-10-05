"""Локальный веб-интерфейс: python -m doctool web  →  http://127.0.0.1:8765

Работает без интернета: страница и скрипты отдаются этим же сервером, внешних CDN нет.
"""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from urllib.parse import quote

import cv2
import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from .integrations import AdminData, JsonFileAdminData, push_to_system
from .jobs import JobManager, result_payload
from .ocr import IMAGE_EXT, OllamaVLM, downscale
from .service import APP_MODES, PAS_MODES, CaseInput
from .verdict import load_case_types

STATIC = Path(__file__).resolve().parent / "static"


def create_app(out_root: str = "results", default_model: str = "qwen3-vl:4b-instruct",
               ollama: str = "http://127.0.0.1:11434") -> FastAPI:
    app = FastAPI(title="doctool", docs_url=None, redoc_url=None)
    jobs = JobManager()
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
                         passport: UploadFile | None = File(None), admin_file: UploadFile | None = File(None)):
        opt = json.loads(options)
        folder = uploads / uuid.uuid4().hex[:12]
        app_path, pas_path = _save(application, folder), _save(passport, folder)
        if not app_path and not pas_path:
            raise HTTPException(400, "Загрузите хотя бы один файл")
        admin = None
        adm_path = _save(admin_file, folder)
        if adm_path:
            admin = JsonFileAdminData(adm_path)
        elif any((opt.get("admin") or {}).values()):
            a = opt["admin"]
            admin = AdminData(fio=a.get("fio", ""), birth_date=a.get("birth_date", ""), passport=a.get("passport", ""),
                              passport_issue_date=a.get("passport_issue_date", ""),
                              passport_issued_by=a.get("passport_issued_by", ""),
                              domains=[d.strip().lower() for d in a.get("domains", "").replace(";", ",").split(",") if d.strip()],
                              source="введено вручную")
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
            ollama=ollama, admin=admin, check_date=opt.get("check_date") or None,
            case_id=opt.get("case_id") or None, out_root=out_root)
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
