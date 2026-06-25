#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional

from PyQt6.QtCore import (
    Qt,
    QThread,
    pyqtSignal,
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    QUrl,
)
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QFileDialog,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableView,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

# ---------------------------
# Категории файлов по расширениям
# ---------------------------
CATEGORIES = {
    "Изображения": {
        ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif",
        ".svg", ".heic", ".raw", ".cr2", ".nef", ".orf", ".arw",
    },
    "Видео": {
        ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
        ".m4v", ".mpeg", ".mpg", ".3gp", ".mts", ".m2ts",
    },
    "Аудио": {
        ".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".ape",
    },
    "Документы": {
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".txt", ".rtf", ".md", ".odt", ".ods", ".odp", ".csv",
    },
    "Архивы": {
        ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".zst",
    },
    "Код": {
        ".py", ".js", ".ts", ".html", ".css", ".java", ".c", ".cpp", ".h",
        ".hpp", ".go", ".rs", ".php", ".rb", ".sh", ".bat", ".ps1",
        ".json", ".yaml", ".yml", ".xml", ".sql",
    },
    "Приложения": {
        ".exe", ".msi", ".apk", ".dmg", ".pkg", ".deb", ".rpm", ".appimage", ".jar",
    },
    "Дисковые образы": {
        ".iso", ".img", ".vhd", ".vhdx", ".nrg", ".bin",
    },
}

EXT_TO_CATEGORY = {
    ext: category
    for category, exts in CATEGORIES.items()
    for ext in exts
}


@dataclass
class FileInfo:
    path: str
    normalized_path: str
    dir_path: str
    name: str
    category: str
    size: int
    mtime: float
    search_blob: str


@dataclass
class FolderStats:
    display: str
    parent: Optional[str]
    count: int = 0
    size_bytes: int = 0


class ScanCanceled(Exception):
    pass


def human_size(num: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if num < 1024 or unit == "PB":
            if unit == "B":
                return f"{num} B"
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} PB"


def format_dt(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "-"


def normalize_compare(path: str) -> str:
    return os.path.normcase(
        os.path.normpath(os.path.abspath(os.path.expanduser(path)))
    )


def classify(path: str) -> str:
    ext = Path(path).suffix.lower()
    return EXT_TO_CATEGORY.get(ext, "Прочее")


def is_within_normalized(child_norm: str, parent_norm: str) -> bool:
    if child_norm == parent_norm:
        return True
    parent_prefix = parent_norm.rstrip(os.sep) + os.sep
    return child_norm.startswith(parent_prefix)


def path_depth(display_path: str, root_display: str) -> int:
    try:
        rel = os.path.relpath(display_path, root_display)
        if rel == os.curdir:
            return 0
        return rel.count(os.sep) + 1
    except Exception:
        return 0


def folder_label(display_path: str, root_display: str) -> str:
    if normalize_compare(display_path) == normalize_compare(root_display):
        return display_path
    name = os.path.basename(os.path.normpath(display_path))
    return name or display_path


def ensure_folder(
    folder_stats: Dict[str, FolderStats],
    dir_display: str,
    root_key: str,
) -> str:
    key = normalize_compare(dir_display)
    if key not in folder_stats:
        parent_key = None if key == root_key else normalize_compare(os.path.dirname(dir_display))
        folder_stats[key] = FolderStats(display=dir_display, parent=parent_key)
    else:
        folder_stats[key].display = dir_display
        if key == root_key:
            folder_stats[key].parent = None
    return key


def scan_disk(
    root_path: str,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    stop_requested: Optional[Callable[[], bool]] = None,
):
    root_display = os.path.normpath(os.path.abspath(os.path.expanduser(root_path)))
    root_key = normalize_compare(root_display)

    files: list[FileInfo] = []
    folder_stats: Dict[str, FolderStats] = {
        root_key: FolderStats(display=root_display, parent=None)
    }

    total_files = 0
    total_bytes = 0
    last_emit_files = 0
    last_emit_time = time.monotonic()

    def should_stop() -> bool:
        return bool(stop_requested and stop_requested())

    def emit_progress(path_hint: str, force: bool = False):
        nonlocal last_emit_files, last_emit_time
        if progress_callback is None:
            return

        now = time.monotonic()
        if force or (total_files - last_emit_files >= 200) or (now - last_emit_time >= 0.2):
            progress_callback(total_files, total_bytes, path_hint)
            last_emit_files = total_files
            last_emit_time = now

    for dirpath, _, filenames in os.walk(root_display, onerror=lambda e: None, followlinks=False):
        if should_stop():
            raise ScanCanceled()

        dir_display = os.path.normpath(dirpath)
        dir_key = ensure_folder(folder_stats, dir_display, root_key)

        # Обновляем прогресс даже если в папке нет файлов
        emit_progress(dir_display)

        for filename in filenames:
            if should_stop():
                raise ScanCanceled()

            full_path = os.path.normpath(os.path.join(dir_display, filename))

            try:
                st = os.stat(full_path)
            except OSError:
                continue

            size = st.st_size
            mtime = st.st_mtime
            category = classify(full_path)
            normalized_path = normalize_compare(full_path)

            files.append(
                FileInfo(
                    path=full_path,
                    normalized_path=normalized_path,
                    dir_path=dir_display,
                    name=filename,
                    category=category,
                    size=size,
                    mtime=mtime,
                    search_blob=f"{filename} {full_path} {category}".lower(),
                )
            )

            total_files += 1
            total_bytes += size

            folder_stats[dir_key].count += 1
            folder_stats[dir_key].size_bytes += size

            emit_progress(full_path)

    # Проталкиваем статистику снизу вверх
    for key, stats in sorted(
        folder_stats.items(),
        key=lambda item: path_depth(item[1].display, root_display),
        reverse=True,
    ):
        if stats.parent and stats.parent in folder_stats:
            parent = folder_stats[stats.parent]
            parent.count += stats.count
            parent.size_bytes += stats.size_bytes

    emit_progress(root_display, force=True)

    return files, folder_stats, total_files, total_bytes, root_display


class FileTableModel(QAbstractTableModel):
    headers = ["Имя", "Путь", "Категория", "Размер", "Изменён"]

    def __init__(self, files=None):
        super().__init__()
        self._files = files or []

    def setFiles(self, files):
        self.beginResetModel()
        self._files = files
        self.endResetModel()

    def fileAt(self, row: int) -> Optional[FileInfo]:
        if 0 <= row < len(self._files):
            return self._files[row]
        return None

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._files)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation == Qt.Orientation.Horizontal
            and 0 <= section < len(self.headers)
        ):
            return self.headers[section]
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None

        file = self.fileAt(index.row())
        if file is None:
            return None

        col = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return file.name
            if col == 1:
                return file.path
            if col == 2:
                return file.category
            if col == 3:
                return human_size(file.size)
            if col == 4:
                return format_dt(file.mtime)

        if role == Qt.ItemDataRole.ToolTipRole:
            return file.path

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col in (3, 4):
                return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            return Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter

        if role == Qt.ItemDataRole.UserRole:
            if col == 0:
                return file.name.lower()
            if col == 1:
                return file.path.lower()
            if col == 2:
                return file.category.lower()
            if col == 3:
                return file.size
            if col == 4:
                return file.mtime

        return None


class FileFilterProxyModel(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self._text_filter = ""
        self._folder_filter_norm = ""
        self.setSortRole(Qt.ItemDataRole.UserRole)

    def set_text_filter(self, text: str):
        self._text_filter = (text or "").strip().lower()
        self.invalidateFilter()

    def set_folder_filter(self, folder: str):
        self._folder_filter_norm = normalize_compare(folder) if folder else ""
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row, source_parent):
        model = self.sourceModel()
        if model is None or not hasattr(model, "fileAt"):
            return True

        file = model.fileAt(source_row)
        if file is None:
            return False

        if self._folder_filter_norm:
            if not is_within_normalized(file.normalized_path, self._folder_filter_norm):
                return False

        if not self._text_filter:
            return True

        return self._text_filter in file.search_blob

    def lessThan(self, left, right):
        model = self.sourceModel()
        if model is None:
            return super().lessThan(left, right)

        l = model.data(left, self.sortRole())
        r = model.data(right, self.sortRole())

        if l is None:
            return True
        if r is None:
            return False

        try:
            return l < r
        except TypeError:
            return str(l) < str(r)


class ScanWorker(QThread):
    scanFinished = pyqtSignal(object)
    scanFailed = pyqtSignal(str)
    scanCanceled = pyqtSignal()
    progress = pyqtSignal(int, int, str)

    def __init__(self, root_path: str):
        super().__init__()
        self.root_path = root_path

    def request_stop(self):
        self.requestInterruption()

    def run(self):
        try:
            payload = scan_disk(
                self.root_path,
                progress_callback=self.progress.emit,
                stop_requested=self.isInterruptionRequested,
            )
        except ScanCanceled:
            self.scanCanceled.emit()
        except Exception as e:
            self.scanFailed.emit(str(e))
        else:
            self.scanFinished.emit(payload)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("Сканер диска — PyQt6")
        self.resize(1280, 760)

        self.worker: Optional[ScanWorker] = None
        self.current_root = ""
        self.current_folder = ""

        self.model = FileTableModel([])
        self.proxy = FileFilterProxyModel()
        self.proxy.setSourceModel(self.model)

        self._build_ui()
        self._apply_style()

        self.summary_label.setText("Выберите папку и нажмите «Сканировать».")
        self.statusBar().showMessage("Готово")

    def _apply_style(self):
        QApplication.setStyle("Fusion")
        self.setStyleSheet("""
            QMainWindow {
                background: #f5f7fb;
            }
            QLabel {
                color: #1f2937;
            }
            QLineEdit {
                padding: 7px 10px;
                border: 1px solid #cfd8e3;
                border-radius: 8px;
                background: white;
            }
            QPushButton {
                padding: 7px 14px;
                border-radius: 8px;
                background: #e9eef5;
                border: 1px solid #cfd8e3;
            }
            QPushButton:hover {
                background: #dde6f2;
            }
            QPushButton:disabled {
                color: #94a3b8;
                background: #f1f5f9;
            }
            QTreeWidget, QTableView {
                background: white;
                border: 1px solid #d9e1eb;
                alternate-background-color: #f8fafc;
            }
            QHeaderView::section {
                background: #eaf0f8;
                padding: 6px;
                border: 0px;
                border-bottom: 1px solid #d9e1eb;
                border-right: 1px solid #d9e1eb;
                font-weight: 600;
            }
            QProgressBar {
                border: 1px solid #d9e1eb;
                border-radius: 6px;
                text-align: center;
                background: white;
                height: 16px;
            }
            QProgressBar::chunk {
                background-color: #4f8cff;
                border-radius: 6px;
            }
        """)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.setSpacing(10)

        # --- Верхняя панель ---
        top_panel = QVBoxLayout()
        top_panel.setSpacing(8)

        path_row = QHBoxLayout()
        path_row.setSpacing(8)

        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("Путь к диску или папке, например: C:\\ или /home/user")
        self.path_edit.setText(os.path.expanduser("~"))
        self.path_edit.setClearButtonEnabled(True)

        self.browse_btn = QPushButton("Обзор...")
        self.browse_btn.clicked.connect(self.choose_path)

        self.scan_btn = QPushButton("Сканировать")
        self.scan_btn.clicked.connect(self.start_scan)

        self.cancel_btn = QPushButton("Отмена")
        self.cancel_btn.clicked.connect(self.cancel_scan)
        self.cancel_btn.setEnabled(False)

        path_row.addWidget(QLabel("Путь:"))
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(self.browse_btn)
        path_row.addWidget(self.scan_btn)
        path_row.addWidget(self.cancel_btn)

        search_row = QHBoxLayout()
        search_row.setSpacing(8)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Поиск по имени, пути или категории")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self.on_search_changed)

        search_row.addWidget(QLabel("Поиск:"))
        search_row.addWidget(self.search_edit, 1)

        self.summary_label = QLabel()
        self.summary_label.setStyleSheet("font-weight: 600; padding: 2px 0;")
        self.summary_label.setWordWrap(True)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setFormat("Готово")

        top_panel.addLayout(path_row)
        top_panel.addLayout(search_row)
        top_panel.addWidget(self.summary_label)
        top_panel.addWidget(self.progress)

        root_layout.addLayout(top_panel)

        # --- Разделитель: дерево папок и таблица файлов ---
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Левое дерево
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)

        left_label = QLabel("Дерево папок")
        left_label.setStyleSheet("font-weight: 600;")
        left_layout.addWidget(left_label)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Папка", "Файлов", "Размер"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.itemSelectionChanged.connect(self.on_tree_selection_changed)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)

        th = self.tree.header()
        th.setStretchLastSection(False)
        th.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        th.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        th.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)

        left_layout.addWidget(self.tree)

        # Правая таблица
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)

        right_label = QLabel("Все файлы")
        right_label.setStyleSheet("font-weight: 600;")
        right_layout.addWidget(right_label)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.setWordWrap(False)
        self.table.doubleClicked.connect(self.open_file_from_table)

        hh = self.table.horizontalHeader()
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)

        self.table.verticalHeader().setVisible(False)

        right_layout.addWidget(self.table)

        splitter.addWidget(left_widget)
        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([320, 900])

        root_layout.addWidget(splitter, 1)

        # Начальная сортировка
        self.table.sortByColumn(3, Qt.SortOrder.DescendingOrder)

    def set_scanning_state(self, scanning: bool):
        self.path_edit.setEnabled(not scanning)
        self.browse_btn.setEnabled(not scanning)
        self.scan_btn.setEnabled(not scanning)
        self.search_edit.setEnabled(not scanning)
        self.tree.setEnabled(not scanning)
        self.table.setEnabled(not scanning)
        self.cancel_btn.setEnabled(scanning)

    def choose_path(self):
        start_dir = self.path_edit.text().strip()
        if not os.path.isdir(start_dir):
            start_dir = os.path.expanduser("~")

        path = QFileDialog.getExistingDirectory(self, "Выберите диск или папку", start_dir)
        if path:
            self.path_edit.setText(path)

    def clear_results(self):
        self.current_root = ""
        self.current_folder = ""
        self.model.setFiles([])
        self.tree.clear()
        self.proxy.set_folder_filter("")
        self.proxy.set_text_filter(self.search_edit.text())
        self.statusBar().showMessage("Готово")

    def start_scan(self):
        if self.worker and self.worker.isRunning():
            return

        root = self.path_edit.text().strip()
        if not root:
            QMessageBox.warning(self, "Нет пути", "Сначала выберите диск или папку.")
            return

        if not os.path.exists(root):
            QMessageBox.critical(self, "Ошибка", f"Путь не найден:\n{root}")
            return

        if not os.path.isdir(root):
            QMessageBox.critical(self, "Ошибка", f"Указанный путь не является папкой:\n{root}")
            return

        self.clear_results()
        self.set_scanning_state(True)

        self.progress.setRange(0, 0)
        self.progress.setFormat("Сканирование...")
        self.summary_label.setText("Сканирование... подождите")
        self.statusBar().showMessage("Сканирование...")

        self.worker = ScanWorker(root)
        self.worker.progress.connect(self.on_scan_progress)
        self.worker.scanFinished.connect(self.on_scan_finished)
        self.worker.scanFailed.connect(self.on_scan_failed)
        self.worker.scanCanceled.connect(self.on_scan_canceled)
        self.worker.start()

    def cancel_scan(self):
        if self.worker and self.worker.isRunning():
            self.worker.request_stop()
            self.progress.setFormat("Остановка...")
            self.statusBar().showMessage("Остановка сканирования...")

    def on_scan_progress(self, files_scanned: int, bytes_scanned: int, current_path: str):
        self.progress.setFormat(
            f"Проверено: {files_scanned} файлов | {human_size(bytes_scanned)}"
        )
        self.statusBar().showMessage(f"Сканирование: {current_path}")

    def _finalize_worker(self):
        if self.worker is not None:
            self.worker.deleteLater()
            self.worker = None

    def on_scan_failed(self, message: str):
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setFormat("Ошибка")
        self.set_scanning_state(False)
        self.summary_label.setText("Ошибка сканирования.")
        self.statusBar().showMessage("Ошибка сканирования")
        self._finalize_worker()
        QMessageBox.critical(self, "Ошибка сканирования", message)

    def on_scan_canceled(self):
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setFormat("Отменено")
        self.set_scanning_state(False)
        self.summary_label.setText("Сканирование отменено.")
        self.statusBar().showMessage("Сканирование отменено")
        self._finalize_worker()

    def build_tree(self, folder_stats: Dict[str, FolderStats], root_display: str):
        self.tree.blockSignals(True)
        self.tree.clear()

        nodes = {}
        root_key = normalize_compare(root_display)
        root_stats = folder_stats.get(
            root_key,
            FolderStats(display=root_display, parent=None, count=0, size_bytes=0),
        )

        root_item = QTreeWidgetItem([
            root_stats.display,
            str(root_stats.count),
            human_size(root_stats.size_bytes),
        ])
        root_item.setData(0, Qt.ItemDataRole.UserRole, root_stats.display)

        font = root_item.font(0)
        font.setBold(True)
        root_item.setFont(0, font)
        root_item.setFont(1, font)
        root_item.setFont(2, font)

        self.tree.addTopLevelItem(root_item)
        nodes[root_key] = root_item

        def ensure_item(display: str):
            key = normalize_compare(display)
            if key in nodes:
                item = nodes[key]
                stats = folder_stats.get(key)
                if stats:
                    item.setText(0, folder_label(stats.display, root_display) if key != root_key else stats.display)
                    item.setText(1, str(stats.count))
                    item.setText(2, human_size(stats.size_bytes))
                    item.setData(0, Qt.ItemDataRole.UserRole, stats.display)
                return item

            if key == root_key:
                return root_item

            stats = folder_stats.get(key)
            actual_display = stats.display if stats else os.path.normpath(display)
            parent_display = os.path.dirname(actual_display)
            parent_item = ensure_item(parent_display)

            count = stats.count if stats else 0
            size_bytes = stats.size_bytes if stats else 0

            item = QTreeWidgetItem([
                folder_label(actual_display, root_display),
                str(count),
                human_size(size_bytes),
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, actual_display)
            parent_item.addChild(item)
            nodes[key] = item
            return item

        try:
            for _, stats in sorted(
                folder_stats.items(),
                key=lambda item: (
                    path_depth(item[1].display, root_display),
                    item[1].display.lower(),
                ),
            ):
                ensure_item(stats.display)

            root_item.setExpanded(True)
            self.tree.expandToDepth(1)
        finally:
            self.tree.blockSignals(False)

        self.tree.setCurrentItem(root_item)

    def on_scan_finished(self, payload):
        files, folder_stats, total_files, total_bytes, root_display = payload

        self.current_root = root_display
        self.current_folder = root_display

        self.model.setFiles(files)
        self.build_tree(folder_stats, root_display)

        self.proxy.set_folder_filter(root_display)
        self.proxy.set_text_filter(self.search_edit.text())

        header = self.table.horizontalHeader()
        section = header.sortIndicatorSection()
        order = header.sortIndicatorOrder()
        if section < 0:
            section = 3
            order = Qt.SortOrder.DescendingOrder
        self.table.sortByColumn(section, order)

        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.progress.setFormat("Готово")
        self.set_scanning_state(False)

        self.summary_label.setText(
            f"Файлов: {total_files} | Папок: {len(folder_stats)} | Общий размер: {human_size(total_bytes)}"
        )
        self.statusBar().showMessage(
            f"Папка: {root_display} | Показано: {self.proxy.rowCount()} из {self.model.rowCount()} файлов"
        )

        self._finalize_worker()

    def on_tree_selection_changed(self):
        if not self.current_root:
            return

        items = self.tree.selectedItems()
        folder = self.current_root

        if items:
            folder = items[0].data(0, Qt.ItemDataRole.UserRole) or self.current_root

        self.current_folder = folder
        self.proxy.set_folder_filter(folder)
        self.update_visible_info()

    def on_search_changed(self, text: str):
        self.proxy.set_text_filter(text)
        self.update_visible_info()

    def update_visible_info(self):
        if not self.current_root:
            return

        total = self.model.rowCount()
        visible = self.proxy.rowCount()
        folder = self.current_folder or self.current_root

        self.statusBar().showMessage(
            f"Папка: {folder} | Показано: {visible} из {total} файлов"
        )

    def open_file_from_table(self, index):
        if not index.isValid():
            return

        source_index = self.proxy.mapToSource(index)
        file = self.model.fileAt(source_index.row())
        if not file:
            return

        if not os.path.exists(file.path):
            QMessageBox.warning(self, "Файл не найден", f"Файл больше не существует:\n{file.path}")
            return

        ok = QDesktopServices.openUrl(QUrl.fromLocalFile(file.path))
        if not ok:
            QMessageBox.warning(self, "Не удалось открыть", f"Не удалось открыть файл:\n{file.path}")

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            reply = QMessageBox.question(
                self,
                "Сканирование идёт",
                "Сканирование ещё выполняется. Остановить и выйти?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return

            self.worker.request_stop()
            self.worker.wait()

        event.accept()


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
