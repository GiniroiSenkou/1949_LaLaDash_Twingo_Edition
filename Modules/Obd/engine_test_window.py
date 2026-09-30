from __future__ import annotations
import os
import re
import uuid
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QIntValidator
from PyQt6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from Modules.Modding.audio_engine import AudioEngine, _HAS_PYDUB
from Modules.Obd._modern_qss import (
    M, modern_button, set_role, set_surface,
)
from Modules.Obd.obd_simulator import OBDSimulatorWindow
from preferences import DATA_SETTINGS

SUPPORTED_AUDIO_EXTS = (".wav", ".mp3") if _HAS_PYDUB else (".wav",)
SUPPORTED_AUDIO_HINT = "Drop .wav or .mp3 files here" if _HAS_PYDUB else "Drop .wav files here (mp3 needs pydub)"

CATEGORY_META: Dict[str, Dict[str, object]] = {
    "engine_on": {
        "label": "Engine On",
        "hint": "Drop loop files and set the RPM for each band.",
        "requires_value": True,
        "value_label": "RPM",
        "min_value": 0,
        "max_value": 20000,
    },
    "engine_off": {
        "label": "Engine Off",
        "hint": "Drop off-throttle loop files and set the RPM for each band. Saved as engine_off_<rpm>.",
        "requires_value": True,
        "value_label": "RPM",
        "min_value": 0,
        "max_value": 20000,
    },
    "turbo": {
        "label": "Turbo",
        "hint": "Drop turbo loop files. Display/save names become turbo_1, turbo_2, ...",
        "requires_value": False,
    },
    "pop": {
        "label": "Pop",
        "hint": "Drop pop / backfire files. Display/save names become pop_1, pop_2, ...",
        "requires_value": False,
    },
    "flutter": {
        "label": "Flutter",
        "hint": "Drop flutter files. Display/save names become flutter_1, flutter_2, ...",
        "requires_value": False,
    },
    "engine_start": {
        "label": "Engine Start",
        "hint": "Optional start-up one-shot. Saved as engine_start.",
        "requires_value": False,
    },
    "transmission": {
        "label": "Transmission",
        "hint": "Optional transmission loop. Saved as transmission.",
        "requires_value": False,
    },
}


def canonical_target_stem(category: str, index: int, value_text: str = "") -> str:
    category = str(category or "").strip().lower()
    value_text = str(value_text or "").strip()
    if category == "engine_on":
        return f"engine_on_{value_text}" if value_text else "engine_on_???"
    if category in ("engine_off", "coast"):
        return f"engine_off_{value_text}" if value_text else "engine_off_???"
    if category in ("transmission", "engine_start"):
        return category
    if not category:
        raise ValueError("Missing category.")
    return f"{category}_{index + 1}"


def suggest_engine_on_rpms(rpm_min: int, rpm_max: int, count: int) -> List[int]:
    rpm_min = max(1, int(rpm_min))
    rpm_max = max(rpm_min, int(rpm_max))
    count = max(1, int(count))
    if count == 1:
        return [rpm_min]
    if rpm_min == rpm_max:
        return [rpm_min] * count

    ratio = (float(rpm_max) / float(rpm_min)) ** (1.0 / float(count - 1))
    values_f = [float(rpm_min) * (ratio ** i) for i in range(count)]
    values = [int(round(v)) for v in values_f]
    values[0] = rpm_min
    values[-1] = rpm_max

    # Keep values strictly increasing after rounding.
    for i in range(1, len(values)):
        if values[i] <= values[i - 1]:
            values[i] = values[i - 1] + 1

    # Clamp to max and back-correct if needed.
    values[-1] = rpm_max
    for i in range(len(values) - 2, -1, -1):
        if values[i] >= values[i + 1]:
            values[i] = values[i + 1] - 1
    values[0] = rpm_min
    return values


def infer_value_from_filename(category: str, source_path: str) -> str:
    stem = os.path.splitext(os.path.basename(str(source_path or "")))[0].strip().lower()
    if category == "engine_on":
        m = re.match(r"^engine_on(?:[_\-\s]?)(\d+)$", stem)
        return m.group(1) if m else ""
    if category in ("engine_off", "coast"):
        m = re.match(r"^engine_off(?:[_\-\s]?)(\d+)$", stem)
        if m:
            return m.group(1)
        m = re.match(r"^coast(?:[_\-\s]?)(\d+)$", stem)
        return m.group(1) if m else ""
    return ""


def infer_category_from_filename(source_path: str) -> str:
    stem = os.path.splitext(os.path.basename(str(source_path or "")))[0].strip().lower()
    if re.match(r"^engine_on(?:[_\-\s]?)(\d+)$", stem):
        return "engine_on"
    if re.match(r"^engine_off(?:[_\-\s]?)(\d+)$", stem):
        return "engine_off"
    if re.match(r"^coast(?:[_\-\s]?)(\d+)$", stem):
        return "engine_off"  # legacy coast_ files route to the engine_off panel
    for category in ("turbo", "pop", "flutter", "engine_start", "transmission"):
        if stem == category or re.match(rf"^{re.escape(category)}(?:[_\-\s]?)(\d+)$", stem):
            return category
    return ""


def infer_index_order_from_filename(category: str, source_path: str) -> Optional[int]:
    """For indexed categories, suffix number chooses relative order."""
    if category not in {"turbo", "pop", "flutter", "engine_start", "transmission"}:
        return None
    stem = os.path.splitext(os.path.basename(str(source_path or "")))[0].strip().lower()
    if category in {"engine_start", "transmission"} and stem == category:
        return 1
    m = re.match(rf"^{re.escape(category)}(?:[_\-\s]?)(\d+)$", stem)
    if not m:
        return None
    try:
        idx = int(m.group(1))
    except Exception:
        return None
    return idx if idx > 0 else None


def build_rename_plan(entries: List[Dict[str, str]]) -> List[Dict[str, str]]:
    per_category_index: Dict[str, int] = {}
    plan: List[Dict[str, str]] = []
    seen_targets: Dict[str, str] = {}

    for entry in entries:
        category = str(entry.get("category") or "").strip().lower()
        source_path = os.path.abspath(str(entry.get("source_path") or "").strip())
        value_text = str(entry.get("value") or "").strip()
        if category not in CATEGORY_META:
            raise ValueError(f"Unsupported category: {category}")
        if not source_path or not os.path.exists(source_path):
            raise ValueError(f"Missing source file: {source_path}")

        index = per_category_index.get(category, 0)
        per_category_index[category] = index + 1
        target_stem = canonical_target_stem(category, index, value_text)
        if "???" in target_stem:
            label = str(CATEGORY_META[category].get("value_label") or "Value")
            raise ValueError(f"{label} is required for {os.path.basename(source_path)}.")

        ext = os.path.splitext(source_path)[1]
        target_path = os.path.join(os.path.dirname(source_path), target_stem + ext)
        norm_target = os.path.normcase(os.path.normpath(target_path))
        norm_source = os.path.normcase(os.path.normpath(source_path))

        existing = seen_targets.get(norm_target)
        if existing and existing != norm_source:
            raise ValueError(f"Rename collision: {os.path.basename(target_path)}")
        if os.path.exists(target_path) and norm_target != norm_source:
            raise ValueError(f"Target already exists: {target_path}")

        seen_targets[norm_target] = norm_source
        plan.append(
            {
                "category": category,
                "source_path": source_path,
                "target_stem": target_stem,
                "target_path": target_path,
            }
        )

    return plan


def rename_audio_files(plan: List[Dict[str, str]]) -> List[Dict[str, str]]:
    staged: List[Dict[str, str]] = []
    try:
        for item in plan:
            source_path = item["source_path"]
            target_path = item["target_path"]
            if os.path.normcase(os.path.normpath(source_path)) == os.path.normcase(os.path.normpath(target_path)):
                continue
            temp_path = os.path.join(
                os.path.dirname(source_path),
                f".enginetest_tmp_{uuid.uuid4().hex}{os.path.splitext(source_path)[1]}",
            )
            os.replace(source_path, temp_path)
            staged.append({"temp_path": temp_path, "source_path": source_path, "target_path": target_path})

        for item in staged:
            os.replace(item["temp_path"], item["target_path"])

        for item in plan:
            item["source_path"] = item["target_path"]
        return plan
    except Exception:
        for item in reversed(staged):
            temp_path = item["temp_path"]
            source_path = item["source_path"]
            if os.path.exists(temp_path) and not os.path.exists(source_path):
                try:
                    os.replace(temp_path, source_path)
                except Exception:
                    pass
        raise


class _DropZone(QFrame):
    filesDropped = pyqtSignal(str, list)

    def __init__(self, category: str, parent=None):
        super().__init__(parent)
        self._category = category
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        mime = event.mimeData()
        if mime and mime.hasUrls():
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event):
        mime = event.mimeData()
        paths = []
        if mime:
            for url in mime.urls():
                if url.isLocalFile():
                    local = url.toLocalFile()
                    if local and os.path.splitext(local)[1].lower() in SUPPORTED_AUDIO_EXTS:
                        paths.append(local)
        if paths:
            self.filesDropped.emit(self._category, paths)
            event.acceptProposedAction()
            return
        event.ignore()


class EngineTestWindow(OBDSimulatorWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Engine Test")
        self.resize(1320, 920)
        self._keep_only_core_metrics()

        self._create_preview_engine()
        self._preview_timer = QTimer(self)
        self._preview_timer.setInterval(50)
        self._preview_timer.timeout.connect(self._preview_tick)
        self._preview_timer.start()

        self._audio_rows: Dict[str, List[Dict[str, object]]] = {category: [] for category in CATEGORY_META}
        self._audio_panels: Dict[str, Dict[str, object]] = {}
        self._row_serial = 0

        self._build_top_mapping_actions()
        self._build_audio_mapping_ui()
        self._update_category_rows("engine_on")
        self._refresh_audio_status("Drop audio files into the category panels to preview them live.")

    def _keep_only_core_metrics(self):
        ordered_keep = ["RPM", "Speed", "Throttle Pos", "Gear"]
        keep = set(ordered_keep)
        removed = [name for name in self._metric_order if name not in keep]
        self._metric_order = [name for name in ordered_keep if name in self._metric_order]

        for name in removed:
            tile = self._metric_tiles.pop(name, None)
            if isinstance(tile, QWidget):
                tile.setParent(None)
                tile.deleteLater()
            self._rows.pop(name, None)
            try:
                self._hub.set_sim_override_enabled(name, False)
            except Exception:
                pass

        self._reflow_metric_tiles(force=True)
        self._sync_sim_enabled()
        # Keep the compact telemetry area tight so the mapping area uses the freed space.
        # One card row exactly — enough that a tile is never clipped, no more.
        self.scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.scroll.setFixedHeight(152)

    def _build_top_mapping_actions(self):
        root = self.layout()
        row = QHBoxLayout()
        row.setSpacing(10)

        self.btn_save_audio = QPushButton(self)
        self.btn_reset_audio = QPushButton(self)
        for btn, txt, role in (
            (self.btn_save_audio, "Save / Rename", "primary"),
            (self.btn_reset_audio, "Reset Test Mapping", None),
        ):
            modern_button(btn, text=txt, role=role)
            row.addWidget(btn)
        row.addStretch(1)

        self.btn_save_audio.clicked.connect(self._save_audio_mapping)
        self.btn_reset_audio.clicked.connect(self._reset_audio_mapping)

        self.lbl_rpm_helper = QLabel("RPM Helper", self)
        set_role(self.lbl_rpm_helper, "field")
        row.addWidget(self.lbl_rpm_helper)

        self.rpm_min_edit = QLineEdit(self)
        self.rpm_min_edit.setPlaceholderText("Min RPM")
        self.rpm_min_edit.setText("800")
        self.rpm_min_edit.setValidator(QIntValidator(1, 30000, self.rpm_min_edit))
        self.rpm_min_edit.setMaximumWidth(96)

        self.rpm_max_edit = QLineEdit(self)
        self.rpm_max_edit.setPlaceholderText("Max RPM")
        self.rpm_max_edit.setText("7000")
        self.rpm_max_edit.setValidator(QIntValidator(1, 30000, self.rpm_max_edit))
        self.rpm_max_edit.setMaximumWidth(96)
        for edit in (self.rpm_min_edit, self.rpm_max_edit):
            edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
            row.addWidget(edit)

        self.btn_engine_on_autofill = QPushButton(self)
        modern_button(self.btn_engine_on_autofill, text="Auto Fill RPM")
        row.addWidget(self.btn_engine_on_autofill)

        self.rpm_min_edit.textChanged.connect(lambda _text: self._update_category_rows("engine_on"))
        self.rpm_max_edit.textChanged.connect(lambda _text: self._update_category_rows("engine_on"))
        self.btn_engine_on_autofill.clicked.connect(self._apply_engine_on_spacing)
        root.insertLayout(1, row)

    def _build_audio_mapping_ui(self):
        root = self.layout()
        section = QFrame(self)
        set_surface(section, "card")
        layout = QVBoxLayout(section)
        layout.setContentsMargins(M["pad"], M["pad"], M["pad"], M["pad"])
        layout.setSpacing(10)

        title = QLabel("Engine Sound Mapping", section)
        set_role(title, "title")
        layout.addWidget(title)

        subtitle = QLabel(
            "Telemetry behavior matches the OBD simulator. The difference here is that audio files are assigned manually and previewed immediately.",
            section,
        )
        subtitle.setWordWrap(True)
        set_role(subtitle, "dim")
        layout.addWidget(subtitle)

        autodetect_row = QHBoxLayout()
        autodetect_row.setSpacing(8)
        self.chk_autodetect_route = QCheckBox("Autodetect category from filename", section)
        self.chk_autodetect_route.setChecked(True)
        autodetect_row.addWidget(self.chk_autodetect_route)
        autodetect_row.addStretch(1)
        layout.addLayout(autodetect_row)

        self.lbl_audio_status = QLabel(section)
        self.lbl_audio_status.setWordWrap(True)
        set_role(self.lbl_audio_status, "faint")
        layout.addWidget(self.lbl_audio_status)

        scroll = QScrollArea(section)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        layout.addWidget(scroll, 1)

        content = QWidget(scroll)
        grid = QGridLayout(content)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(M["gap"])
        grid.setVerticalSpacing(M["gap"])
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        scroll.setWidget(content)

        categories = list(CATEGORY_META.keys())
        cols = 3
        for idx, category in enumerate(categories):
            panel = self._make_category_panel(category, content)
            self._audio_panels[category] = panel
            grid.addWidget(panel["frame"], idx // cols, idx % cols)

        root.addWidget(section, 1)

    def _make_category_panel(self, category: str, parent=None) -> Dict[str, object]:
        meta = CATEGORY_META[category]
        frame = QFrame(parent)
        set_surface(frame, "raised")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)

        title = QLabel(str(meta["label"]), frame)
        set_role(title, "section")
        layout.addWidget(title)

        hint = QLabel(str(meta["hint"]), frame)
        hint.setWordWrap(True)
        set_role(hint, "faint")
        layout.addWidget(hint)

        drop = _DropZone(category, frame)
        set_surface(drop, "dashed")
        drop_l = QVBoxLayout(drop)
        drop_l.setContentsMargins(10, 10, 10, 10)
        drop_l.setSpacing(8)
        empty = QLabel(SUPPORTED_AUDIO_HINT, drop)
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.setWordWrap(True)
        set_role(empty, "ghost")
        drop_l.addWidget(empty)
        rows_wrap = QWidget(drop)
        rows_l = QVBoxLayout(rows_wrap)
        rows_l.setContentsMargins(0, 0, 0, 0)
        rows_l.setSpacing(8)
        drop_l.addWidget(rows_wrap)
        layout.addWidget(drop, 1)
        drop.filesDropped.connect(self._handle_files_dropped)

        panel: Dict[str, object] = {"frame": frame, "rows_layout": rows_l, "empty": empty}
        return panel

    def _refresh_audio_status(self, text: str):
        self.lbl_audio_status.setText(str(text or ""))

    def _create_preview_engine(self):
        self._preview_engine = AudioEngine(settings_path=DATA_SETTINGS)
        self._preview_engine.set_volume(100)
        for feature in ("turbo", "flutter", "pops", "gears", "transmission"):
            self._preview_engine.set_runtime_feature(feature, False)

    def _reset_preview_engine_state(self):
        try:
            self._preview_engine.set_engine_sequence([])
            self._preview_engine.set_engine_off_sequence([])
            self._preview_engine.set_asset_overrides(
                {
                    "turbo": [],
                    "pop": [],
                    "flutter": [],
                    "engine_start": [],
                    "transmission": [],
                }
            )
            for feature in ("turbo", "flutter", "pops", "gears", "transmission"):
                self._preview_engine.set_runtime_feature(feature, False)
            self._preview_engine.set_volume(100)
            if hasattr(self._preview_engine, "_pcm_cache"):
                self._preview_engine._pcm_cache.clear()
            # Ensure stream is alive for next trial.
            if getattr(self._preview_engine, "_stream", None) is None and hasattr(self._preview_engine, "_open_stream"):
                self._preview_engine._open_stream()
        except Exception:
            pass

    def _metric_value(self, name: str, default: float = 0.0) -> float:
        row = self._rows.get(name)
        if isinstance(row, dict):
            try:
                return float(row.get("value", default))
            except Exception:
                pass
        try:
            return float(self._hub.value(name, default))
        except Exception:
            return float(default)

    def _preview_tick(self):
        try:
            if getattr(self._preview_engine, "_stream", None) is None and hasattr(self._preview_engine, "_open_stream"):
                self._preview_engine._open_stream()
            rpm = max(0, int(round(self._metric_value("RPM", 800.0))))
            speed = max(0.0, float(self._metric_value("Speed", 0.0)))
            gear = int(round(self._metric_value("Gear", 0.0)))
            throttle_pct = max(0.0, min(100.0, float(self._metric_value("Throttle Pos", 0.0))))
            throttle = throttle_pct / 100.0
            self._preview_engine.process_sample(rpm=rpm, gear=gear, throttle=throttle, speed=speed)
        except Exception:
            pass

    def _engine_on_bounds(self) -> tuple[int, int]:
        rpm_min = 800
        rpm_max = 7000
        if isinstance(getattr(self, "rpm_min_edit", None), QLineEdit) and self.rpm_min_edit.text().strip():
            rpm_min = int(self.rpm_min_edit.text().strip())
        if isinstance(getattr(self, "rpm_max_edit", None), QLineEdit) and self.rpm_max_edit.text().strip():
            rpm_max = int(self.rpm_max_edit.text().strip())
        rpm_min = max(1, rpm_min)
        rpm_max = max(rpm_min, rpm_max)
        return rpm_min, rpm_max

    def _apply_engine_on_spacing(self):
        rows = self._audio_rows.get("engine_on", [])
        if not rows:
            self._refresh_audio_status("No engine_on files loaded.")
            return
        rpm_min, rpm_max = self._engine_on_bounds()
        values = suggest_engine_on_rpms(rpm_min, rpm_max, len(rows))
        for row, value in zip(rows, values):
            value_edit = row.get("value_edit")
            if isinstance(value_edit, QLineEdit):
                value_edit.blockSignals(True)
                value_edit.setText(str(value))
                value_edit.blockSignals(False)
        self._update_category_rows("engine_on")
        self._refresh_audio_status("Engine On RPM values auto-filled using geometric spacing.")

    def _has_row_for_path(self, path: str) -> bool:
        norm = os.path.normcase(os.path.normpath(path))
        for rows in self._audio_rows.values():
            for row in rows:
                if os.path.normcase(os.path.normpath(str(row["source_path"]))) == norm:
                    return True
        return False

    def _handle_files_dropped(self, category: str, paths: List[str]):
        added = 0
        skipped = 0
        routed = 0
        touched_categories = set()
        route_by_name = True
        if isinstance(getattr(self, "chk_autodetect_route", None), QCheckBox):
            route_by_name = bool(self.chk_autodetect_route.isChecked())
        for path in paths:
            target_category = (infer_category_from_filename(path) if route_by_name else "") or category
            if target_category not in CATEGORY_META:
                target_category = category
            if self._has_row_for_path(path):
                skipped += 1
                continue
            if target_category != category:
                routed += 1
            self._add_audio_row(target_category, path)
            touched_categories.add(target_category)
            added += 1

        if touched_categories:
            for target in sorted(touched_categories):
                self._sort_category_rows(target)
                self._update_category_rows(target)

        if added and routed:
            self._refresh_audio_status(
                f"Added {added} file(s). Auto-routed {routed} by filename prefix."
            )
        elif added:
            self._refresh_audio_status(f"Added {added} file(s) to {CATEGORY_META[category]['label']}.")
        elif skipped:
            self._refresh_audio_status("Dropped files were already assigned in Engine Test.")

    def _make_remove_button(self, parent=None) -> QPushButton:
        btn = QPushButton(parent)
        modern_button(btn, text="Remove", role="danger", height=30)
        return btn

    def _make_move_button(self, parent=None, text: str = "↑") -> QPushButton:
        btn = QPushButton(parent)
        modern_button(btn, text=text, role="chip", height=30, min_width=32)
        return btn

    def _add_audio_row(self, category: str, path: str):
        meta = CATEGORY_META[category]
        rows_layout = self._audio_panels[category]["rows_layout"]

        frame = QFrame()
        set_surface(frame, "row")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        src = QLabel(os.path.basename(path), frame)
        src.setToolTip(path)
        set_role(src, "section")
        layout.addWidget(src)

        path_lbl = QLabel(path, frame)
        path_lbl.setWordWrap(True)
        set_role(path_lbl, "mono")
        layout.addWidget(path_lbl)

        ctl_row = QHBoxLayout()
        ctl_row.setSpacing(8)
        value_edit = None
        if bool(meta.get("requires_value")):
            value_edit = QLineEdit(frame)
            value_edit.setPlaceholderText(str(meta.get("value_label") or "Value"))
            value_edit.setValidator(QIntValidator(int(meta["min_value"]), int(meta["max_value"]), value_edit))
            value_edit.setMinimumWidth(84)
            value_edit.setMaximumWidth(110)
            value_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
            inferred_value = infer_value_from_filename(category, path)
            if inferred_value:
                value_edit.setText(inferred_value)
            ctl_row.addWidget(value_edit)

        target_lbl = QLabel(frame)
        target_lbl.setWordWrap(True)
        set_role(target_lbl, "dim")
        layout.addWidget(target_lbl)
        ctl_row.addStretch(1)

        move_up_btn = None
        move_down_btn = None
        if category in {"engine_on", "engine_off"}:
            move_up_btn = self._make_move_button(frame, text="↑")
            move_down_btn = self._make_move_button(frame, text="↓")
            ctl_row.addWidget(move_up_btn)
            ctl_row.addWidget(move_down_btn)

        remove_btn = self._make_remove_button(frame)
        ctl_row.addWidget(remove_btn)
        layout.addLayout(ctl_row)

        row = {
            "frame": frame,
            "category": category,
            "source_path": os.path.abspath(path),
            "source_label": src,
            "path_label": path_lbl,
            "value_edit": value_edit,
            "target_label": target_lbl,
            "move_up_btn": move_up_btn,
            "move_down_btn": move_down_btn,
            "order_hint": infer_index_order_from_filename(category, path),
            "serial": self._row_serial,
        }
        self._row_serial += 1
        self._audio_rows[category].append(row)
        rows_layout.addWidget(frame)

        if value_edit is not None:
            value_edit.textChanged.connect(lambda _text, cat=category: self._update_category_rows(cat))
        if isinstance(move_up_btn, QPushButton):
            move_up_btn.clicked.connect(lambda _=False, row_ref=row: self._move_audio_row(row_ref, -1))
        if isinstance(move_down_btn, QPushButton):
            move_down_btn.clicked.connect(lambda _=False, row_ref=row: self._move_audio_row(row_ref, 1))
        remove_btn.clicked.connect(lambda _=False, row_ref=row: self._remove_audio_row(row_ref))

    def _reflow_category_rows_layout(self, category: str):
        rows_layout = self._audio_panels[category]["rows_layout"]
        if not isinstance(rows_layout, QVBoxLayout):
            return
        while rows_layout.count():
            it = rows_layout.takeAt(0)
            w = it.widget()
            if isinstance(w, QWidget):
                w.setParent(None)
        for row in self._audio_rows.get(category, []):
            frame = row.get("frame")
            if isinstance(frame, QWidget):
                rows_layout.addWidget(frame)

    def _sort_category_rows(self, category: str):
        if category not in {"turbo", "pop", "flutter", "engine_start", "transmission"}:
            return
        rows = self._audio_rows.get(category, [])
        if len(rows) < 2:
            return

        def _key(item: Dict[str, object]):
            hint = item.get("order_hint")
            serial = int(item.get("serial") or 0)
            if isinstance(hint, int):
                return (0, hint, serial)
            return (1, 10**9, serial)

        rows.sort(key=_key)
        self._reflow_category_rows_layout(category)

    def _move_audio_row(self, row: Dict[str, object], delta: int):
        category = str(row.get("category") or "")
        if category not in {"engine_on", "engine_off"}:
            return
        rows = self._audio_rows.get(category, [])
        if row not in rows:
            return
        src = rows.index(row)
        dst = src + int(delta)
        if dst < 0 or dst >= len(rows):
            return
        rows[src], rows[dst] = rows[dst], rows[src]
        self._reflow_category_rows_layout(category)
        self._update_category_rows(category)

    def _remove_audio_row(self, row: Dict[str, object]):
        category = str(row["category"])
        rows = self._audio_rows[category]
        if row in rows:
            rows.remove(row)
        frame = row["frame"]
        if isinstance(frame, QWidget):
            frame.setParent(None)
            frame.deleteLater()
        self._update_category_rows(category)
        self._refresh_audio_status(f"Removed file from {CATEGORY_META[category]['label']}.")

    def _update_category_rows(self, category: str):
        rows = self._audio_rows[category]
        empty = self._audio_panels[category]["empty"]
        if isinstance(empty, QLabel):
            empty.setVisible(not rows)
        suggested_values: List[int] = []
        if category == "engine_on" and rows:
            rpm_min, rpm_max = self._engine_on_bounds()
            suggested_values = suggest_engine_on_rpms(rpm_min, rpm_max, len(rows))
        for index, row in enumerate(rows):
            value_edit = row["value_edit"]
            value_text = value_edit.text() if isinstance(value_edit, QLineEdit) else ""
            target = canonical_target_stem(category, index, value_text)
            target_label = row["target_label"]
            if isinstance(target_label, QLabel):
                if category == "engine_on" and index < len(suggested_values):
                    suggested = int(suggested_values[index])
                    matches = False
                    try:
                        matches = int(value_text) == suggested
                    except Exception:
                        matches = False
                    suggested_color = M["ok"] if matches else M["warn"]
                    target_label.setTextFormat(Qt.TextFormat.RichText)
                    target_label.setText(
                        f"Preview name: {target}  "
                        f"<span style='color:{suggested_color};'>Suggested: engine_on_{suggested}</span>"
                    )
                else:
                    target_label.setTextFormat(Qt.TextFormat.PlainText)
                    target_label.setText(f"Preview name: {target}")
        self._rebuild_preview_engine()

    def _collect_mapping_entries(self) -> List[Dict[str, str]]:
        entries: List[Dict[str, str]] = []
        for category, rows in self._audio_rows.items():
            for row in rows:
                value_edit = row["value_edit"]
                value_text = value_edit.text() if isinstance(value_edit, QLineEdit) else ""
                entries.append(
                    {
                        "category": category,
                        "source_path": str(row["source_path"]),
                        "value": value_text,
                    }
                )
        return entries

    def _rebuild_preview_engine(self):
        engine_on: List[Dict[str, object]] = []
        engine_off: List[Dict[str, object]] = []
        overrides: Dict[str, List[str]] = {
            "turbo": [],
            "pop": [],
            "flutter": [],
            "engine_start": [],
            "transmission": [],
        }

        for row in self._audio_rows["engine_on"]:
            value_edit = row["value_edit"]
            if isinstance(value_edit, QLineEdit) and value_edit.text().strip():
                engine_on.append({"file": str(row["source_path"]), "rpm": int(value_edit.text().strip())})
        for row in self._audio_rows["engine_off"]:
            value_edit = row["value_edit"]
            if isinstance(value_edit, QLineEdit) and value_edit.text().strip():
                engine_off.append({"file": str(row["source_path"]), "rpm": int(value_edit.text().strip())})
        for category in overrides:
            overrides[category] = [str(row["source_path"]) for row in self._audio_rows[category]]

        engine_on.sort(key=lambda item: int(item["rpm"]))
        engine_off.sort(key=lambda item: int(item["rpm"]))
        self._preview_engine.set_engine_sequence(engine_on)
        self._preview_engine.set_engine_off_sequence(engine_off)
        self._preview_engine.set_asset_overrides(overrides)
        self._preview_engine.set_runtime_feature("turbo", bool(overrides["turbo"]))
        self._preview_engine.set_runtime_feature("flutter", bool(overrides["flutter"]))
        self._preview_engine.set_runtime_feature("pops", bool(overrides["pop"]))
        self._preview_engine.set_runtime_feature("gears", False)
        self._preview_engine.set_runtime_feature("transmission", bool(overrides["transmission"]))

        self._refresh_audio_status(
            f"Preview updated: on={len(engine_on)} engine_off={len(engine_off)} turbo={len(overrides['turbo'])} "
            f"pop={len(overrides['pop'])} flutter={len(overrides['flutter'])}"
        )

    def _save_audio_mapping(self):
        entries = self._collect_mapping_entries()
        if not entries:
            self._refresh_audio_status("Nothing to save. Drop some audio files first.")
            return

        try:
            plan = build_rename_plan(entries)
            rename_audio_files(plan)
        except Exception as e:
            QMessageBox.warning(self, "Save failed", str(e))
            self._refresh_audio_status(f"Save failed: {e}")
            return

        for category, rows in self._audio_rows.items():
            for index, row in enumerate(rows):
                updated = plan.pop(0)
                row["source_path"] = updated["source_path"]
                src = row["source_label"]
                path_lbl = row["path_label"]
                if isinstance(src, QLabel):
                    src.setText(os.path.basename(str(row["source_path"])))
                    src.setToolTip(str(row["source_path"]))
                if isinstance(path_lbl, QLabel):
                    path_lbl.setText(str(row["source_path"]))
            self._update_category_rows(category)

        self._refresh_audio_status("Save complete. Original files were renamed in place.")

    def _clear_audio_rows_and_preview(self):
        for category, rows in self._audio_rows.items():
            while rows:
                row = rows.pop()
                frame = row["frame"]
                if isinstance(frame, QWidget):
                    frame.setParent(None)
                    frame.deleteLater()
            empty = self._audio_panels[category]["empty"]
            if isinstance(empty, QLabel):
                empty.setVisible(True)
        self._reset_preview_engine_state()

    def _reset_audio_mapping(self):
        self._clear_audio_rows_and_preview()
        if isinstance(getattr(self, "rpm_min_edit", None), QLineEdit):
            self.rpm_min_edit.setText("800")
        if isinstance(getattr(self, "rpm_max_edit", None), QLineEdit):
            self.rpm_max_edit.setText("7000")
        if isinstance(getattr(self, "chk_autodetect_route", None), QCheckBox):
            self.chk_autodetect_route.setChecked(True)
        self._update_category_rows("engine_on")
        self._refresh_audio_status("Engine Test mapping reset.")

    def closeEvent(self, event):
        try:
            if self._preview_timer.isActive():
                self._preview_timer.stop()
        except Exception:
            pass
        try:
            self._preview_engine.close()
        except Exception:
            pass
        return super().closeEvent(event)
