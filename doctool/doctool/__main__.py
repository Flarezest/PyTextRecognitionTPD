"""Командная строка.

Примеры (подробно — docs/USAGE.md):
  python -m doctool gui                                    # окно приложения
  python -m doctool web                                    # веб-интерфейс в браузере
  python -m doctool check -a заявление.pdf -p паспорт.pdf
  python -m doctool --vlm qwen3-vl:4b-instruct check -a заявление.jpg -p паспорт.pdf --handwritten
  python -m doctool extract паспорт.pdf --type passport
  python -m doctool whois example.ru пример.рф             # статус домена в реестре (WHOIS)
  python -m doctool validate                               # проверить forms/*.yaml и config/case_types.yaml
"""
from __future__ import annotations

import sys

if sys.version_info < (3, 10):
    sys.exit(f"Нужен Python 3.10 или новее, сейчас {sys.version.split()[0]}. "
             "Установите Python 3.12 с python.org и запускайте через: py -3.12 -m doctool ...")

import argparse
import json
from pathlib import Path

from .compare import STATUS_RU
from .integrations import JsonFileAdminData
from .ocr import OllamaVLM, check_tesseract, load_document
from .progress import log

DEFAULT_MODEL = "qwen3-vl:4b-instruct"


def _vlm(args) -> OllamaVLM | None:
    if not args.vlm:
        return None
    log(f"Проверяю модель {args.vlm} в Ollama ({args.ollama})…")
    v = OllamaVLM(model=args.vlm, host=args.ollama)
    if not v.available():
        print(f"[!] Модель {args.vlm} недоступна в Ollama ({args.ollama}) — продолжаю без VLM. "
              f"Проверьте, что Ollama запущена и модель скачана: ollama pull {args.vlm}", file=sys.stderr)
        return None
    return v


def cmd_check(args):
    from .service import CaseInput, run_case
    for f in (args.application, args.passport):
        if f and not Path(f).exists():
            sys.exit(f"[!] Файл не найден: {f}")
    if args.handwritten and not args.vlm:
        print("[!] --handwritten без --vlm: рукописные поля попадут в отчёт только фрагментами.", file=sys.stderr)
    admin = JsonFileAdminData(args.admin) if args.admin else None
    inp = CaseInput(case_type=args.case_type, application=args.application, passport=args.passport,
                    combined=args.combined, app_mode=args.app_mode, app_handwritten=args.handwritten,
                    pas_mode=args.passport_mode, vlm_model=args.vlm, ollama=args.ollama, admin=admin,
                    check_date=args.date, case_id=args.case, out_root=args.out)
    res = run_case(inp)
    d = res.decision
    print(f"\nВЕРДИКТ: {d.title.upper()}")
    for r in d.reasons:
        print(f"  — {r}")
    print("\nПроверки:")
    for c in res.checks:
        mark = {"ok": "✓", "warn": "!", "fail": "✗", "review": "?", "info": "·"}[c.status]
        print(f"  {mark} {c.name}: {STATUS_RU[c.status]}" + (f" — {c.detail}" if c.detail else ""))
    print(f"\nПапка дела: {res.case_dir}\n  report.html — отчёт, export.json — выгрузка для системы, "
          f"result.json — всё распознанное")


def cmd_extract(args):
    from .application import extract_application
    from .passport_rf import extract_passport
    if not Path(args.file).exists():
        sys.exit(f"[!] Файл не найден: {args.file}")
    vlm = _vlm(args)
    pages = load_document(args.file)
    if args.type == "passport":
        ex = extract_passport(pages, vlm)
    else:
        ex = extract_application(pages, Path(args.out), vlm)
    print(json.dumps(ex.to_dict(), ensure_ascii=False, indent=2, default=str))


def cmd_web(args):
    from .web import serve
    serve(host=args.host, port=args.port, out_root=args.out, model=args.vlm or DEFAULT_MODEL,
          ollama=args.ollama, open_browser=not args.no_browser)


def cmd_whois(args):
    from .domains import split_domains
    from .whois import lookup
    for d in split_domains(" ".join(args.domains)):
        w = lookup(d)
        print(f"{d}: {w.summary}")
        if args.raw and w.raw:
            print(w.raw)


def cmd_validate(args):
    from . import checks as checklib
    from .formspec import ROLES, load_forms, validate_config
    from .verdict import load_case_types
    types = load_case_types()
    if args.list:
        print("Проверки (id для checks: в config/case_types.yaml):")
        for cid in checklib.ids_for({"form": "*"}):
            d = checklib.REGISTRY[cid]
            extra = (f"; флаги: {', '.join(d.flags)}" if d.flags else "") + \
                    {"passport": "; нужен паспорт", "domains": "; после загрузки из manager"}.get(d.needs or "", "")
            print(f"  {cid:24} {d.title}{extra}")
            for n, rows in d.names.items():
                print(f"  {'':24}   «{n}» — строка таблицы: {', '.join(rows) or '—'}")
        print("\nРоли полей заявления (role: в forms/*.yaml):")
        for r, t in ROLES.items():
            print(f"  {r:24} {t}")
        print("\nТипы заявлений:")
        for tid, ct in types.items():
            print(f"  {tid:24} бланк: {ct.get('form') or '—'}; проверки: {', '.join(checklib.ids_for(ct))}")
        print()
    problems = validate_config(case_types=types)
    for p in problems:
        print(f"[!] {p}")
    if problems:
        sys.exit(f"Найдено проблем: {len(problems)}")
    print(f"Конфигурация в порядке: бланков {len(load_forms())}, типов заявлений {len(types)}, "
          f"проверок {len(checklib.REGISTRY)}.")


def cmd_gui(args):
    from .qt_app import main as qt_main
    qt_main(out_root=args.out, model=args.vlm or DEFAULT_MODEL, ollama=args.ollama)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(prog="doctool", description="Локальная проверка заявлений и паспортов РФ")
    p.add_argument("--vlm", default=None, help=f"модель Ollama для рукописного текста, напр. {DEFAULT_MODEL}")
    p.add_argument("--ollama", default="http://127.0.0.1:11434", help="адрес Ollama")
    p.add_argument("-q", "--quiet", action="store_true", help="не показывать ход работы")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="проверить заявление (и паспорт), вынести вердикт")
    c.add_argument("-a", "--application", help="файл заявления (или общий файл с --combined)")
    c.add_argument("-p", "--passport", help="файл паспорта: PDF (можно весь паспорт) или фото/скан разворота")
    c.add_argument("--combined", action="store_true", help="заявление и паспорт в одном файле (-a)")
    c.add_argument("-t", "--case-type", default="admin_change_person", help="тип заявления из config/case_types.yaml")
    c.add_argument("--app-mode", choices=["auto", "electronic", "scan", "photo"], default="auto",
                   help="какое заявление: электронное, скан, фото (по умолчанию определяется само)")
    c.add_argument("--handwritten", action="store_true", help="заявление заполнено от руки (нужен --vlm)")
    c.add_argument("--passport-mode", choices=["auto", "scan", "photo"], default="auto", help="паспорт: скан или фото")
    c.add_argument("--admin", help="JSON с данными текущего администратора из внутренней системы")
    c.add_argument("-o", "--out", default="results", help="папка для результатов (по умолчанию results)")
    c.add_argument("--case", help="название дела (по умолчанию — имя файла)")
    c.add_argument("--date", help=argparse.SUPPRESS)   # с 0.5.0 не используется: срок действия паспорта не проверяется
    c.set_defaults(func=cmd_check)

    e = sub.add_parser("extract", help="только извлечь поля из документа")
    e.add_argument("file", help="файл документа")
    e.add_argument("--type", choices=["passport", "application"], default="application",
                   help="тип документа (по умолчанию application — заявление)")
    e.add_argument("-o", "--out", default="results", help="куда сохранять фрагменты сканов (crops)")
    e.set_defaults(func=cmd_extract)

    w = sub.add_parser("web", help="запустить веб-интерфейс (откроется в браузере)")
    w.add_argument("--host", default="127.0.0.1", help="0.0.0.0 — открыть доступ из локальной сети")
    w.add_argument("--port", type=int, default=8765)
    w.add_argument("-o", "--out", default="results")
    w.add_argument("--no-browser", action="store_true", help="не открывать браузер автоматически")
    w.set_defaults(func=cmd_web)

    g = sub.add_parser("gui", help="запустить окно приложения (Qt)")
    g.add_argument("-o", "--out", default="results")
    g.set_defaults(func=cmd_gui)

    wh = sub.add_parser("whois", help="статус домена в реестре (WHOIS, порт 43)")
    wh.add_argument("domains", nargs="+", help="домены")
    wh.add_argument("--raw", action="store_true", help="показать ответ сервера целиком")
    wh.set_defaults(func=cmd_whois)

    v = sub.add_parser("validate", help="проверить описания бланков (forms/) и типов заявлений (config/)")
    v.add_argument("--list", action="store_true", help="показать проверки, роли полей и типы заявлений")
    v.set_defaults(func=cmd_validate)

    args = p.parse_args(argv)
    import doctool.progress as _pr
    _pr.quiet = args.quiet
    if args.cmd == "check" and not args.application and not args.passport:
        p.error("укажите -a и/или -p")
    err = check_tesseract() if args.cmd not in ("whois", "validate") else None
    if err:
        sys.exit("[!] " + err)
    args.func(args)


if __name__ == "__main__":
    main()
