"""Окно приложения (PySide6): python -m doctool gui

Логика та же, что в веб-интерфейсе и командной строке (service.run_case / recompute).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog,
                               QFileDialog, QFormLayout, QFrame, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                               QRadioButton, QScrollArea, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
                               QVBoxLayout, QWidget)

from . import progress
from .compare import STATUS_RU
from .integrations import push_to_system
from .service import APP_MODES, PAS_MODES, CaseInput, CaseResult, recompute, run_case, table_rows
from .verdict import load_case_types

STATUS_COLORS = {"ok": "#e5f4ea", "warn": "#fff3cd", "fail": "#fde4e2", "review": "#e3eefb", "info": "#efefef"}
DECISION_STYLE = {"accept": "background:#e5f4ea;color:#1e6b3a", "reject": "background:#fde4e2;color:#9b1c1c",
                  "review": "background:#fff3cd;color:#7a5b00"}
SRC_RU = {"text": "текст", "ocr": "OCR", "mrz": "MRZ", "red-ocr": "красные цифры", "vlm": "модель",
          "manual": "исправлено вручную"}
FILE_FILTER = "Документы (*.pdf *.docx *.jpg *.jpeg *.png *.tif *.tiff *.bmp *.webp)"


def _src(s: str) -> str:
    for k, v in SRC_RU.items():
        s = s.replace(k, v) if s.startswith(k) else s
    return s.replace("+рукопись", " + рукопись")


def open_path(p: str | Path):
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(p).resolve())))


# ------------------------------------------------------------------ фоновая задача

class Worker(QObject):
    message = Signal(str)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, inp: CaseInput):
        super().__init__()
        self.inp = inp
        self.cancel = threading.Event()

    def run(self):
        progress.set_sink(self.message.emit, self.cancel)
        try:
            self.finished.emit(run_case(self.inp))
        except progress.Cancelled:
            self.failed.emit("Остановлено пользователем")
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"Ошибка: {e}")
        finally:
            progress.set_sink(None, None)


# ------------------------------------------------------------------ виджеты

class DropBox(QGroupBox):
    """Область для файла: перетаскивание или кнопка «Выбрать…», плюс флаги."""
    changed = Signal(str)

    def __init__(self, title: str, modes: dict, extra_checkbox: str):
        super().__init__(title)
        self.path: str | None = None
        self.setAcceptDrops(True)
        lay = QVBoxLayout(self)
        self.label = QLabel("Перетащите файл сюда или нажмите «Выбрать…»")
        self.label.setWordWrap(True)
        self.label.setStyleSheet("color:#667085")
        row = QHBoxLayout()
        pick = QPushButton("Выбрать…")
        pick.clicked.connect(self.pick)
        self.pick_btn = pick
        self.file_enabled = True
        row.addWidget(self.label, 1)
        row.addWidget(pick)
        lay.addLayout(row)
        self.modes = QButtonGroup(self)
        mrow = QHBoxLayout()
        for i, (k, v) in enumerate(modes.items()):
            rb = QRadioButton(v)
            rb.setProperty("mode", k)
            rb.setChecked(i == 0)
            self.modes.addButton(rb)
            mrow.addWidget(rb)
        mrow.addStretch()
        lay.addLayout(mrow)
        self.extra = QCheckBox(extra_checkbox)
        lay.addWidget(self.extra)
        self.setStyleSheet("QGroupBox{border:2px dashed #c9ced6;border-radius:8px;margin-top:10px;padding:8px}"
                           "QGroupBox::title{subcontrol-origin:margin;left:10px;padding:0 4px;font-weight:600}")

    def mode(self) -> str:
        b = self.modes.checkedButton()
        return b.property("mode") if b else "auto"

    def set_file(self, p: str):
        self.path = p
        self.label.setText(f"<b>{Path(p).name}</b><br><span style='color:#667085'>{Path(p).parent}</span>")
        self.changed.emit(p)

    def pick(self):
        p, _ = QFileDialog.getOpenFileName(self, "Выберите файл", "", FILE_FILTER)
        if p:
            self.set_file(p)

    def set_file_enabled(self, on: bool, note: str = ""):
        """В режиме «общий файл» паспортная область без файла, но флаги остаются доступны."""
        self.file_enabled = on
        self.pick_btn.setEnabled(on)
        if on:
            self.label.setText(f"<b>{Path(self.path).name}</b>" if self.path else "Перетащите файл сюда или нажмите «Выбрать…»")
        else:
            self.label.setText(note)

    def dragEnterEvent(self, e):
        if self.file_enabled and e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        urls = e.mimeData().urls()
        if urls:
            self.set_file(urls[0].toLocalFile())


class ImageDialog(QDialog):
    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(Path(path).name)
        lay = QVBoxLayout(self)
        lbl = QLabel()
        pm = QPixmap(path)
        screen = QApplication.primaryScreen().availableGeometry()
        lbl.setPixmap(pm.scaled(int(screen.width() * 0.85), int(screen.height() * 0.8),
                                Qt.KeepAspectRatio, Qt.SmoothTransformation))
        lay.addWidget(lbl)


class ClickLabel(QLabel):
    clicked = Signal()

    def mousePressEvent(self, e):
        self.clicked.emit()


# ------------------------------------------------------------------ главное окно

class MainWindow(QMainWindow):
    def __init__(self, out_root: str, model: str, ollama: str, llm_model: str | None = None):
        super().__init__()
        self.out_root, self.default_model, self.ollama = out_root, model, ollama
        from .llm_extract import DEFAULT_MODEL as LLM_DEFAULT
        self.default_llm_model = llm_model or LLM_DEFAULT
        self.res: CaseResult | None = None
        self.thread: QThread | None = None
        self.worker: Worker | None = None
        self.setWindowTitle("doctool — проверка заявлений")
        self.resize(1400, 900)
        split = QSplitter(Qt.Horizontal)
        split.addWidget(self._build_left())
        split.addWidget(self._build_right())
        split.setSizes([460, 940])
        self.setCentralWidget(split)
        self.statusBar().showMessage(self._ollama_status())

    # ---------------- левая панель: ввод
    def _build_left(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        form = QFormLayout()
        self.case_type = QComboBox()
        for k, v in load_case_types().items():
            self.case_type.addItem(v["title"] + ("" if v.get("enabled", True) else " (скоро)"), k)
            if not v.get("enabled", True):
                self.case_type.model().item(self.case_type.count() - 1).setEnabled(False)
        self.operation = QComboBox()
        self.operation.addItem("Сверка и выгрузка данных для системы", "check_export")
        self.operation.addItem("Автозаполнение во внутренней системе (позже)", "autofill")
        self.operation.model().item(1).setEnabled(False)
        self.case_id = QLineEdit()
        self.case_id.setPlaceholderText("необязательно, напр. Иванов_домен.рф")
        form.addRow("Тип заявления", self.case_type)
        form.addRow("Операция", self.operation)
        form.addRow("Название дела", self.case_id)
        lay.addLayout(form)

        self.combined = QCheckBox("Заявление и паспорт в одном файле")
        self.combined.toggled.connect(self._toggle_combined)
        lay.addWidget(self.combined)
        self.app_box = DropBox("Заявление", APP_MODES, "Рукописное — читать локальной моделью (qwen)")
        self.pas_box = DropBox("Паспорт", PAS_MODES, "Дочитывать плохо распознанные поля моделью")
        self.llm_fields = QCheckBox("Поля заявления — нейросетью (вместо шаблона бланка)")
        self.llm_fields.setToolTip("Без галки поля читаются по шаблону бланка (регулярками) — для типовых печатных "
                                   "заявлений. С галкой — нейросетью: свободная форма, бланк нотариуса, новые типы.")
        self.app_box.layout().addWidget(self.llm_fields)
        lay.addWidget(self.app_box)
        lay.addWidget(self.pas_box)

        mrow = QHBoxLayout()
        mrow.addWidget(QLabel("Модель для рукописи"))
        self.model = QComboBox()
        self.model.setEditable(True)
        models = self._ollama_models() or [self.default_model]
        self.model.addItems(models)
        if self.default_model in models:
            self.model.setCurrentText(self.default_model)
        mrow.addWidget(self.model, 1)
        lay.addLayout(mrow)
        mrow = QHBoxLayout()
        mrow.addWidget(QLabel("Модель для полей"))
        self.llm_model = QComboBox()
        self.llm_model.setEditable(True)
        lm = self._ollama_models()
        self.llm_model.addItems(lm if self.default_llm_model in lm else [self.default_llm_model] + lm)
        self.llm_model.setCurrentText(self.default_llm_model)
        mrow.addWidget(self.llm_model, 1)
        lay.addLayout(mrow)

        brow = QHBoxLayout()
        self.run_btn = QPushButton("Проверить")
        self.run_btn.setStyleSheet("QPushButton{background:#2b6cb0;color:white;font-weight:600;padding:8px 18px;border-radius:6px}"
                                   "QPushButton:disabled{background:#9db6d3}")
        self.run_btn.clicked.connect(self.start)
        self.stop_btn = QPushButton("Остановить")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop)
        brow.addWidget(self.run_btn)
        brow.addWidget(self.stop_btn)
        brow.addStretch()
        lay.addLayout(brow)
        self.busy = QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.setVisible(False)
        lay.addWidget(self.busy)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("Здесь будет ход работы")
        self.log.setStyleSheet("font-family:Consolas,monospace;font-size:12px")
        lay.addWidget(self.log, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(w)
        return scroll

    # ---------------- правая панель: результат
    def _build_right(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.verdict = QLabel("Загрузите документы и нажмите «Проверить»")
        self.verdict.setWordWrap(True)
        self.verdict.setStyleSheet("padding:14px;border-radius:8px;background:#f0f2f5;color:#444;font-size:14px")
        lay.addWidget(self.verdict)
        self.tabs = QTabWidget()
        # поля
        fields = QWidget()
        fl = QVBoxLayout(fields)
        hint = QLabel("Значения можно исправить двойным щелчком. «Подтверждаю» — вы сверили поле по документу. "
                      "После правок нажмите «Пересчитать».")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#667085")
        fl.addWidget(hint)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Поле", "Заявление", "Паспорт", "Фрагмент заявления", "Статус", "Подтверждаю"])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.Fixed)
        self.table.setColumnWidth(3, 230)
        hh.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(True)
        fl.addWidget(self.table, 1)
        arow = QHBoxLayout()
        for text, fn in (("Пересчитать", self.do_recompute), ("Сохранить выгрузку…", self.save_export),
                         ("Открыть отчёт", self.open_report), ("Открыть папку дела", self.open_dir),
                         ("Отправить во внутреннюю систему", self.push)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            arow.addWidget(b)
            if text == "Пересчитать":
                b.setStyleSheet("font-weight:600")
        arow.addStretch()
        fl.addLayout(arow)
        self.tabs.addTab(fields, "Поля")
        # проверки
        self.checks = QTableWidget(0, 5)
        self.checks.setHorizontalHeaderLabels(["Проверка", "Статус", "Заявление", "Паспорт", "Комментарий"])
        ch = self.checks.horizontalHeader()
        for i in range(5):
            ch.setSectionResizeMode(i, QHeaderView.Stretch if i == 4 else QHeaderView.ResizeToContents)
        self.checks.verticalHeader().setVisible(False)
        self.checks.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tabs.addTab(self.checks, "Все проверки")
        # страницы общего файла
        self.pages_tab = QWidget()
        pl = QVBoxLayout(self.pages_tab)
        pl.addWidget(QLabel("Проверьте, где заявление, а где паспорт, и при необходимости перезапустите."))
        self.pages_row = QHBoxLayout()
        pages_w = QWidget()
        pages_w.setLayout(self.pages_row)
        ps = QScrollArea()
        ps.setWidgetResizable(True)
        ps.setWidget(pages_w)
        pl.addWidget(ps, 1)
        rerun = QPushButton("Перезапустить с этими страницами")
        rerun.clicked.connect(self.rerun_pages)
        pl.addWidget(rerun)
        self.tabs.addTab(self.pages_tab, "Страницы")
        # выгрузка и замечания
        self.record = QPlainTextEdit()
        self.record.setReadOnly(True)
        self.record.setStyleSheet("font-family:Consolas,monospace;font-size:12px")
        self.tabs.addTab(self.record, "Выгрузка (JSON)")
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.tabs.addTab(self.notes, "Замечания")
        lay.addWidget(self.tabs, 1)
        self.page_role_boxes: dict[int, QComboBox] = {}
        return w

    # ---------------- служебное
    def _ollama_models(self) -> list[str]:
        try:
            with urllib.request.urlopen(f"{self.ollama}/api/tags", timeout=2) as r:
                return [m["name"] for m in json.load(r).get("models", [])]
        except Exception:  # noqa: BLE001
            return []

    def _ollama_status(self) -> str:
        m = self._ollama_models()
        return (f"Ollama: доступно моделей {len(m)}" if m
                else "Ollama не найдена — рукописные поля будут только во фрагментах для ручной сверки")

    def _toggle_combined(self, on: bool):
        self.pas_box.set_file_enabled(not on, "Паспорт — в общем файле выше. Флаги ниже относятся к его страницам.")
        self.app_box.setTitle("Общий файл: заявление + паспорт" if on else "Заявление")

    # ---------------- запуск
    def _input(self, page_roles=None) -> CaseInput | None:
        combined = self.combined.isChecked()
        app, pas = self.app_box.path, (None if combined else self.pas_box.path)
        if not app and not pas:
            QMessageBox.information(self, "doctool", "Загрузите файл заявления и/или паспорта.")
            return None
        hand = self.app_box.extra.isChecked()
        use_vlm = hand or self.pas_box.extra.isChecked()
        return CaseInput(case_type=self.case_type.currentData(), application=app, passport=pas, combined=combined,
                         page_roles=page_roles, app_mode=self.app_box.mode(), app_handwritten=hand,
                         pas_mode=self.pas_box.mode(), vlm_model=self.model.currentText() if use_vlm else None,
                         ollama=self.ollama,
                         case_id=self.case_id.text().strip() or None, out_root=self.out_root,
                         llm_fields=self.llm_fields.isChecked(), llm_model=self.llm_model.currentText().strip() or None)

    def start(self, _=None, page_roles=None):
        inp = self._input(page_roles)
        if inp is None:
            return
        self.log.clear()
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.busy.setVisible(True)
        self.verdict.setText("Идёт проверка…")
        self.verdict.setStyleSheet("padding:14px;border-radius:8px;background:#f0f2f5;color:#444;font-size:14px")
        self.thread = QThread(self)
        self.worker = Worker(inp)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.message.connect(self.log.appendPlainText)
        self.worker.finished.connect(self._done)
        self.worker.failed.connect(self._failed)
        self.worker.finished.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)
        self.thread.start()

    def stop(self):
        if self.worker:
            self.worker.cancel.set()
            self.log.appendPlainText("Останавливаю…")

    def _finish_ui(self):
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.busy.setVisible(False)

    def _failed(self, msg: str):
        self._finish_ui()
        self.log.appendPlainText(msg)
        self.verdict.setText(msg)

    def _done(self, res: CaseResult):
        self._finish_ui()
        self.res = res
        self.render()

    # ---------------- отображение
    def render(self):
        res = self.res
        d = res.decision
        self.verdict.setText(f"<span style='font-size:20px;font-weight:700'>{d.title}</span>"
                             f" &nbsp;·&nbsp; {res.case_id} · {res.case_type.get('title', '')}<br>"
                             + "<br>".join("— " + r for r in d.reasons))
        self.verdict.setStyleSheet(f"padding:14px;border-radius:8px;font-size:14px;{DECISION_STYLE[d.code]}")
        rows = table_rows(res)
        self.rows = rows
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            title = QTableWidgetItem(r["title"])
            title.setFlags(Qt.ItemIsEnabled)
            title.setToolTip("\n".join(r["details"]))
            self.table.setItem(i, 0, title)
            for col, key, src, editable in ((1, "app", "app_source", r["editable_app"]),
                                            (2, "passport", "passport_source", r["editable_passport"])):
                it = QTableWidgetItem(r[key] if editable else "—")
                it.setData(Qt.UserRole, r[key])
                it.setToolTip(f"источник: {_src(r[src])}" if r[src] else "")
                if not editable:
                    it.setFlags(Qt.ItemIsEnabled)
                self.table.setItem(i, col, it)
            crop = ClickLabel()
            if r.get("crop") and Path(r["crop"]).exists():
                pm = QPixmap(r["crop"])
                crop.setPixmap(pm.scaled(220, 60, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                crop.setCursor(Qt.PointingHandCursor)
                crop.clicked.connect(lambda p=r["crop"]: ImageDialog(p, self).exec())
            self.table.setCellWidget(i, 3, crop)
            st = QTableWidgetItem(STATUS_RU.get(r["status"], "") if r["status"] else "")
            st.setFlags(Qt.ItemIsEnabled)
            if r["status"]:
                from PySide6.QtGui import QColor
                st.setBackground(QColor(STATUS_COLORS[r["status"]]))
            self.table.setItem(i, 4, st)
            cb = QTableWidgetItem()
            cb.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            cb.setCheckState(Qt.Checked if r["confirmed"] else Qt.Unchecked)
            self.table.setItem(i, 5, cb)
            self.table.setRowHeight(i, 66 if r.get("crop") else 34)
        # проверки
        from PySide6.QtGui import QColor
        self.checks.setRowCount(len(res.checks))
        for i, c in enumerate(res.checks):
            vals = [c.name, STATUS_RU[c.status], c.application, c.passport, c.detail]
            for j, v in enumerate(vals):
                it = QTableWidgetItem(v or "")
                if j == 1:
                    it.setBackground(QColor(STATUS_COLORS[c.status]))
                self.checks.setItem(i, j, it)
        # страницы
        while self.pages_row.count():
            item = self.pages_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.page_role_boxes = {}
        if res.page_roles:
            self._render_pages(res)
        self.tabs.setTabEnabled(2, bool(res.page_roles))
        self.record.setPlainText(json.dumps(res.record, ensure_ascii=False, indent=2, default=str))
        notes = list(res.notes) + (res.app.notes if res.app else []) + (res.pas.notes if res.pas else [])
        self.notes.setPlainText("\n".join("• " + n for n in notes) or "нет")
        self.tabs.setCurrentIndex(0)

    def _render_pages(self, res: CaseResult):
        import pymupdf
        src = Path(res.inp.application or res.inp.passport)
        doc = pymupdf.open(src) if src.suffix.lower() == ".pdf" else None
        for idx, role in sorted(res.page_roles.items()):
            box = QFrame()
            box.setFrameShape(QFrame.StyledPanel)
            bl = QVBoxLayout(box)
            img = QLabel()
            if doc is not None and idx < doc.page_count:
                pm = QPixmap()
                pm.loadFromData(doc[idx].get_pixmap(dpi=40).tobytes("png"))
                img.setPixmap(pm.scaledToHeight(140, Qt.SmoothTransformation))
            bl.addWidget(img)
            bl.addWidget(QLabel(f"стр. {idx + 1}"))
            cb = QComboBox()
            for k, t in (("application", "заявление"), ("passport", "паспорт"), ("skip", "пропустить")):
                cb.addItem(t, k)
            cb.setCurrentIndex(max(0, cb.findData(role)))
            bl.addWidget(cb)
            self.page_role_boxes[idx] = cb
            self.pages_row.addWidget(box)
        self.pages_row.addStretch()

    # ---------------- действия с результатом
    def do_recompute(self):
        if not self.res:
            return
        overrides, confirmed = {}, []
        for i, r in enumerate(self.rows):
            for col, key in ((1, "app"), (2, "passport")):
                it = self.table.item(i, col)
                if it is not None and it.flags() & Qt.ItemIsEditable and it.text() != (it.data(Qt.UserRole) or ""):
                    overrides.setdefault(r["id"], {})[key] = it.text()
            if self.table.item(i, 5).checkState() == Qt.Checked:
                confirmed.append(r["id"])
        progress.set_sink(lambda m: None, None)
        try:
            recompute(self.res, overrides, confirmed)
        finally:
            progress.set_sink(None, None)
        self.render()
        self.statusBar().showMessage("Пересчитано", 4000)

    def save_export(self):
        if not self.res:
            return
        p, _ = QFileDialog.getSaveFileName(self, "Сохранить выгрузку", f"{self.res.case_id}_export.json", "JSON (*.json)")
        if p:
            Path(p).write_text(json.dumps(self.res.record, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            self.statusBar().showMessage(f"Сохранено: {p}", 5000)

    def open_report(self):
        if self.res:
            open_path(Path(self.res.case_dir) / "report.html")

    def open_dir(self):
        if self.res:
            open_path(self.res.case_dir)

    def push(self):
        if not self.res:
            return
        try:
            push_to_system(self.res.record)
        except NotImplementedError as e:
            QMessageBox.information(self, "Внутренняя система", f"{e}.\nПока используйте «Сохранить выгрузку…».")

    def rerun_pages(self):
        if not self.page_role_boxes:
            return
        self.start(page_roles={i: cb.currentData() for i, cb in self.page_role_boxes.items()})


def main(out_root: str = "results", model: str = "qwen3-vl:4b-instruct", ollama: str = "http://127.0.0.1:11434",
         llm_model: str | None = None):
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("doctool")
    w = MainWindow(out_root, model, ollama, llm_model)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
