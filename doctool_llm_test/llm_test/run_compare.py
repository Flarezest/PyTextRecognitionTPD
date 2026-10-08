"""Прогон наборов в двух режимах — регулярки бланка и модель — и сводка отличий (0.7.0-dev).

    python llm_test/run_compare.py [--model qwen3:8b] [--think on|off] [--dataset ПАПКА] [--sets set1 set8 …]
                                   [--skip-regex] [--timeout 240]

Наборы — подпапки ПАПКИ (по умолчанию Dataset\\PhysicalPersonAdminChange), как в check_all.bat:
  Application* и Passport* — заявление и паспорт; только Passport* — «Только проверка паспорта»;
  иначе — первый PDF/JPG/PNG как заявление; set6 — заявление f8639148…pdf, паспорт doc0401…pdf.
Результаты: results_cmp\\regex\\<набор>, results_cmp\\llm\\<набор>, сводка — results_cmp\\llm\\сравнение.html.
Ответы модели кэшируются (results_cmp\\llm_cache): повторный прогон того же текста не обращается к модели.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASETS = [Path(r"F:\Programming\Python\PyTextRecognition\Dataset\PhysicalPersonAdminChange"),
                    ROOT.parent.parent / "Dataset" / "PhysicalPersonAdminChange"]


def sort_key(p: Path):
    digits = "".join(ch for ch in p.name if ch.isdigit())
    return (int(digits) if digits else 10 ** 6, p.name)


def args_for(folder: Path) -> list[str] | None:
    files = sorted(f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in (".pdf", ".jpg", ".jpeg", ".png"))
    app = [f for f in files if f.name.startswith("Application")]
    pas = [f for f in files if f.name.startswith("Passport")]
    if folder.name == "set6":
        a = [f for f in files if f.name.startswith("f8639148")]
        p = [f for f in files if f.name.startswith("doc0401")]
        if a and p:
            return ["-a", str(a[0]), "-p", str(p[0])]
    if app and pas:
        return ["-a", str(app[0]), "-p", str(pas[0])]
    if pas:
        return ["-t", "passport_only", "-p", str(pas[0])]
    return ["-a", str(files[0])] if files else None


def ollama_check(host: str, model: str) -> str | None:
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=5) as r:
            names = [m.get("name", "") for m in json.load(r).get("models", [])]
    except Exception as e:  # noqa: BLE001
        return f"Ollama не отвечает по адресу {host} ({e}). Запустите Ollama."
    want = model if ":" in model else model + ":latest"
    if want not in names:
        return f"Модели {model} нет в Ollama. Скачайте: ollama pull {model}   (есть: {', '.join(names) or 'ничего'})"
    return None


def run(cmd: list[str], env: dict, log: Path) -> tuple[int, int]:
    t = time.time()
    r = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    sec = round(time.time() - t)
    log.write_text((r.stdout or "") + "\n--- stderr ---\n" + (r.stderr or "") + f"\n--- {sec} s\n", encoding="utf-8")
    return r.returncode, sec


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="qwen3:8b")
    ap.add_argument("--think", choices=["on", "off", "default"], default="on")
    ap.add_argument("--dataset", default=None)
    ap.add_argument("--sets", nargs="*", default=None)
    ap.add_argument("--skip-regex", action="store_true", help="не прогонять режим регулярок (уже есть)")
    ap.add_argument("--timeout", type=int, default=240, help="предел на одно обращение к модели, с")
    ap.add_argument("--host", default="http://127.0.0.1:11434")
    ap.add_argument("--out", default="results_cmp")
    a = ap.parse_args()
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")

    ds = Path(a.dataset) if a.dataset else next((p for p in DEFAULT_DATASETS if p.is_dir()), None)
    if ds is None or not ds.is_dir():
        sys.exit("Не найдена папка наборов. Укажите: --dataset F:\\...\\Dataset\\PhysicalPersonAdminChange")
    err = ollama_check(a.host.rstrip("/"), a.model)
    if err:
        sys.exit("[!] " + err)
    out = (ROOT / a.out).resolve()
    sets = sorted((p for p in ds.iterdir() if p.is_dir() and (not a.sets or p.name in a.sets)), key=sort_key)
    print(f"Наборы: {ds}  ({len(sets)})\nМодель: {a.model}, рассуждение: {a.think}, предел {a.timeout} с\n"
          f"Результаты: {out}\n")

    base_env = {k: v for k, v in os.environ.items() if not k.startswith("DOCTOOL_LLM_")}
    llm_env = dict(base_env, DOCTOOL_LLM_FIELDS=a.model, DOCTOOL_LLM_HOST=a.host, DOCTOOL_LLM_TIMEOUT=str(a.timeout),
                   DOCTOOL_LLM_CACHE=str(out / "llm_cache"))
    if a.think != "default":
        llm_env["DOCTOOL_LLM_THINK"] = "1" if a.think == "on" else "0"
    total = {"regex": 0, "llm": 0}
    for folder in sets:
        args = args_for(folder)
        if not args:
            print(f"{folder.name:7} — нет файлов")
            continue
        modes = (["regex"] if not a.skip_regex else []) + ["llm"]
        line = [f"{folder.name:7}"]
        for mode in modes:
            d = out / mode
            d.mkdir(parents=True, exist_ok=True)
            cmd = [sys.executable, "-m", "doctool", "-q", "check", *args, "--case", folder.name, "-o", str(d / folder.name)]
            rc, sec = run(cmd, llm_env if mode == "llm" else base_env, d / f"{folder.name}.log")
            total[mode] += sec
            line.append(f"{mode}: {sec:>4} с" + ("" if rc == 0 else f" (ошибка {rc}, см. {mode}\\{folder.name}.log)"))
        print("  ".join(line), flush=True)
    print(f"\nВсего: регулярки {total['regex']} с, модель {total['llm']} с (включая распознавание сканов)\n")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import compare_llm
    compare_llm.main([str(out / "regex"), str(out / "llm"), "-o", str(out / "llm" / "сравнение.html")])


if __name__ == "__main__":
    main()
