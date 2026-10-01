from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import errno
import os
from pathlib import Path
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from PySide6.QtCore import Qt, QThread, Signal, QUrl, QTimer, QSize
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QComboBox, QCheckBox, QFrame, QTreeWidget,
    QTreeWidgetItem, QHeaderView, QFileDialog, QProgressBar, QScrollArea,
    QAbstractItemView, QMenu, QMessageBox, QToolButton)

from engine import INPUTS, OUTPUTS, Options, convert, Cancelled

APP_VERSION = '0.1.0'
LOG_DIR = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'AudioConvert' / 'logs'
LOG_DIR.mkdir(parents=True, exist_ok=True)
handler = RotatingFileHandler(LOG_DIR / 'AudioConvert.log', maxBytes=2_000_000, backupCount=2, encoding='utf-8')
logging.basicConfig(level=logging.INFO, handlers=[handler], format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('AudioConvert')


def explain_failure(error: str | BaseException, output: Path | None = None) -> str:
    message = str(error).strip()
    lower = message.lower()
    winerror = getattr(error, 'winerror', None)
    error_number = getattr(error, 'errno', None)
    detail = message[-600:] if message else '没有收到详细错误信息。'

    if lower.startswith('kgg') or lower.startswith('暂不支持 kgg'):
        return message
    if winerror == 112 or error_number == errno.ENOSPC or any(
            marker in lower for marker in ('no space left on device', 'disk full', 'not enough space', 'errno 28')):
        cause = '输出位置的磁盘空间不足。请清理磁盘空间后重试。'
    elif winerror in (5, 13) or error_number in (errno.EACCES, errno.EPERM) or any(
            marker in lower for marker in ('permission denied', 'access is denied', '拒绝访问')):
        cause = '没有读取源文件或写入输出文件夹的权限。请检查文件夹权限。'
    elif error_number == errno.ENAMETOOLONG or any(
            marker in lower for marker in ('filename or extension is too long', 'path too long', '路径过长')):
        cause = '文件路径过长。请将文件移到较短的目录后重试。'
    elif any(marker in lower for marker in ('无法识别音频流', '无效数据', 'invalid data',
                                            'corrupt', '损坏', 'error while decoding')):
        cause = '源文件可能已损坏，或文件扩展名与实际音频格式不匹配。'
    elif any(marker in lower for marker in ('不支持的输入格式', '不支持的输出格式',
                                            '格式不受支持', 'unknown encoder', 'unsupported codec',
                                            'not supported', 'unsupported format')):
        cause = '音频编码或格式不受支持。请尝试 MP3、WAV 等常见格式。'
    elif any(marker in lower for marker in ('没有那个文件', '系统找不到指定的文件',
                                            'no such file', 'cannot find the path', 'cannot open',
                                            'i/o error', 'input/output error')):
        cause = '源文件、输出文件夹或磁盘当前不可用。请确认文件仍存在且磁盘已连接。'
    elif output is not None:
        try:
            if output.exists() and output.is_dir():
                import shutil
                space = shutil.disk_usage(output)
                if space.free < 1024 * 1024:
                    cause = '输出位置剩余空间不足。请清理磁盘空间后重试。'
                else:
                    cause = '转换失败，可能是音频编码不兼容或源文件异常。'
            else:
                cause = '输出位置无法访问或创建。请检查文件夹路径和写入权限。'
        except OSError:
            cause = '无法读取输出位置。请检查目标磁盘和文件夹状态。'
    else:
        cause = '转换失败，可能是音频编码不兼容或源文件异常。'

    if detail and detail != cause:
        return cause + '\n详细信息：' + detail
    return cause


def app_icon() -> QIcon:
    pixmap = QPixmap(128, 128)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor('#202a27'))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(4, 4, 120, 120, 28, 28)
    painter.setPen(QPen(QColor('#74efbb'), 9, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    for x, height in [(31, 20), (48, 44), (65, 68), (82, 42), (99, 18)]:
        painter.drawLine(x, 64-height//2, x, 64+height//2)
    painter.end()
    return QIcon(pixmap)


@dataclass
class AudioItem:
    source: Path
    root: Path | None
    size: int
    duration: float = 0
    state: str = 'pending'
    output: Path | None = None
    detail: str = ''


def pretty_duration(value: float) -> str:
    if not value:
        return '—'
    total = int(value)
    return f'{total//3600}:{total//60%60:02d}:{total%60:02d}' if total >= 3600 else f'{total//60:02d}:{total%60:02d}'


class SelectBox(QComboBox):
    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = '#a3b0a8' if QApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark else '#728176'
        painter.setPen(QPen(QColor(color), 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        x, y = self.width() - 18, self.height() // 2
        painter.drawLine(x-4, y-2, x, y+2)
        painter.drawLine(x, y+2, x+4, y-2)
        painter.end()


class Scanner(QThread):
    ready = Signal(object)
    failed = Signal(str)
    def __init__(self, paths: list[Path], recursive: bool, exclude: Path | None):
        super().__init__()
        self.paths, self.recursive, self.exclude = paths, recursive, exclude
        self.cancel = threading.Event()

    def run(self):
        items, seen = [], set()
        errors = []
        def add(path, root):
            if self.cancel.is_set() or path.suffix.lower() not in INPUTS or path.is_symlink():
                return
            try:
                path = path.resolve(strict=True)
                if self.exclude and path.is_relative_to(self.exclude):
                    return
                if path in seen or not path.is_file():
                    return
                seen.add(path)
                duration = 0
                if path.suffix.lower() not in ('.xm', '.kgg'):
                    try:
                        import mutagen
                        info = mutagen.File(path)
                        duration = getattr(getattr(info, 'info', None), 'length', 0) or 0
                    except Exception:
                        pass
                items.append(AudioItem(path, root, path.stat().st_size, duration))
            except OSError as error:
                errors.append(str(error))
        try:
            for path in self.paths:
                if self.cancel.is_set():
                    break
                if path.is_dir():
                    root = path.resolve()
                    for folder, dirs, names in os.walk(root, followlinks=False, onerror=lambda e: errors.append(str(e))):
                        if self.cancel.is_set():
                            break
                        dirs[:] = sorted([d for d in dirs if not (Path(folder)/d).is_symlink()
                                          and not (Path(folder)/d).is_junction()
                                          and (not self.exclude or not (Path(folder)/d).resolve().is_relative_to(self.exclude))]) if self.recursive else []
                        for name in sorted(names):
                            if self.cancel.is_set():
                                break
                            add(Path(folder)/name, root)
                else:
                    add(path, None)
            self.ready.emit(items)
            if errors:
                self.failed.emit('部分文件无法读取：\n' + '\n'.join(errors[:4]))
        except Exception as error:
            log.exception('Scan failed')
            self.failed.emit(str(error))


class Converter(QThread):
    status = Signal(int, str, int)
    result = Signal(int, object, str)
    def __init__(self, jobs: list[tuple[int, AudioItem]], options: Options, concurrency: int = 2):
        super().__init__()
        self.jobs, self.options = jobs, options
        self.concurrency = concurrency
        self.cancel = threading.Event()

    def run(self):
        def process(index, item):
            try:
                result = convert(item.source, item.root, self.options, self.cancel,
                                 lambda name, progress, i=index: self.status.emit(i, name, progress))
                self.result.emit(index, result, '')
            except Cancelled:
                self.result.emit(index, None, '已取消，源文件保留')
            except Exception as error:
                log.exception('Conversion failed: %s', item.source)
                self.result.emit(index, None, str(error))

        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            pending = iter(self.jobs)
            active = set()
            while True:
                while not self.cancel.is_set() and len(active) < self.concurrency:
                    try:
                        index, item = next(pending)
                    except StopIteration:
                        break
                    active.add(pool.submit(process, index, item))
                if not active:
                    break
                finished = next(as_completed(active))
                active.remove(finished)
                finished.result()


class DropArea(QFrame):
    filesDropped = Signal(object)
    choose = Signal()
    def __init__(self):
        super().__init__()
        self.setObjectName('dropArea')
        self.setAcceptDrops(True)
        self.setMinimumHeight(205)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        picture = QLabel()
        pm = QPixmap(58, 48)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('#7f8d85'), 2.5))
        painter.drawRoundedRect(4, 12, 50, 32, 4, 4)
        painter.drawLine(5, 12, 5, 5)
        painter.drawLine(5, 5, 22, 5)
        painter.drawLine(22, 5, 29, 12)
        painter.end()
        picture.setPixmap(pm)
        picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel('拖入音频文件或文件夹')
        title.setObjectName('dropTitle')
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        formats = QLabel('MP3 · M4A · FLAC · WAV · OGG · XM · KGG 等')
        formats.setProperty('muted', True)
        formats.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(picture)
        layout.addSpacing(13)
        layout.addWidget(title)
        layout.addWidget(formats)

    def dragEnterEvent(self, event):
        if self.isEnabled() and event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        self.filesDropped.emit([Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()])
        event.acceptProposedAction()

    def mouseDoubleClickEvent(self, event):
        self.choose.emit()


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('AudioConvert')
        self.setWindowIcon(app_icon())
        self.resize(1040, 740)
        self.setMinimumSize(980, 720)
        self.items: list[AudioItem] = []
        self.worker = None
        self.scanner = None
        self.closing = False
        self.filter = 'all'
        self.completed_count = 0
        self.batch_count = 0
        self.build()
        self.apply_theme()
        QApplication.styleHints().colorSchemeChanged.connect(lambda *_: self.apply_theme())
        self.refresh()

    def build(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        header = QFrame()
        header.setObjectName('header')
        h = QHBoxLayout(header)
        h.setContentsMargins(27, 17, 27, 17)
        logo = QLabel()
        logo.setPixmap(app_icon().pixmap(28, 28))
        h.addWidget(logo)
        title = QLabel('AudioConvert')
        title.setObjectName('brand')
        h.addWidget(title)
        h.addStretch()
        self.theme_label = QLabel('主题：跟随系统')
        self.theme_label.setProperty('muted', True)
        h.addWidget(self.theme_label)
        outer.addWidget(header)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        outer.addLayout(body, 1)

        queue_panel = QWidget()
        left = QVBoxLayout(queue_panel)
        left.setContentsMargins(27, 27, 27, 16)
        left.setSpacing(17)
        head = QHBoxLayout()
        heading = QLabel('转换队列')
        heading.setObjectName('sectionTitle')
        head.addWidget(heading)
        head.addSpacing(10)
        self.add_button = QPushButton('＋  添加文件')
        self.add_button.clicked.connect(self.choose_files)
        head.addWidget(self.add_button)
        head.addStretch()
        left.addLayout(head)
        tabs = QHBoxLayout()
        tabs.setSpacing(6)
        self.tabs = []
        for text, value in [('全部', 'all'), ('已完成', 'done'), ('未成功', 'error')]:
            b = QPushButton(text + '  0')
            b.setObjectName('tab')
            b.setCheckable(True)
            b.setChecked(value == 'all')
            b.clicked.connect(lambda checked=False, v=value: self.change_filter(v))
            self.tabs.append(b)
            tabs.addWidget(b)
        tabs.addStretch()
        self.clear_button = QPushButton('清空')
        self.clear_button.setObjectName('quiet')
        self.clear_button.clicked.connect(self.clear_items)
        tabs.addWidget(self.clear_button)
        left.addLayout(tabs)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['文件名', '格式', '时长', '状态'])
        self.tree.setRootIsDecorated(False)
        self.tree.setIndentation(0)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col, width in [(1, 66), (2, 66), (3, 128)]:
            self.tree.header().setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            self.tree.setColumnWidth(col, width)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.context_menu)
        self.tree.itemDoubleClicked.connect(self.open_item)
        self.tree.itemSelectionChanged.connect(self.update_controls)
        left.addWidget(self.tree, 1)
        self.drop = DropArea()
        self.drop.filesDropped.connect(self.scan)
        self.drop.choose.connect(self.choose_files)
        left.addWidget(self.drop, 2)
        small = QLabel('本地处理，无需上传音频')
        small.setProperty('muted', True)
        left.addWidget(small)
        body.addWidget(queue_panel, 7)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        # Keep settings readable at minimum size; expand both panels at 7:3.
        scroll.setMinimumWidth(355)
        side = QWidget()
        side.setObjectName('side')
        scroll.setWidget(side)
        right = QVBoxLayout(side)
        right.setContentsMargins(25, 27, 25, 24)
        right.setSpacing(12)
        heading = QLabel('转换设置')
        heading.setObjectName('sectionTitle')
        right.addWidget(heading)
        right.addSpacing(7)
        self.source, self.source_button = self.folder_field(right, '原音频文件夹', '选择原音频文件夹', self.choose_source)
        self.recursive = QCheckBox('包含子文件夹')
        self.recursive.setChecked(True)
        self.recursive.toggled.connect(self.rescan_source)
        right.addWidget(self.recursive)
        right.addSpacing(3)
        self.output, self.output_button = self.folder_field(right, '输出文件夹', '选择输出文件夹', self.choose_output)
        self.line(right)
        right.addWidget(QLabel('输出格式'))
        self.format = SelectBox()
        self.format.addItems(OUTPUTS)
        self.format.setCurrentText('MP3')
        right.addWidget(self.format)
        right.addWidget(QLabel('音频质量'))
        self.quality = SelectBox()
        for label, rate in [('优先原音质 · 兼容时快速转换', 0), ('标准 · 192 kbps', 192), ('语音 · 128 kbps', 128), ('高品质 · 320 kbps', 320)]:
            self.quality.addItem(label, rate)
        right.addWidget(self.quality)
        self.advanced_toggle = QToolButton()
        self.advanced_toggle.setText('高级设置')
        self.advanced_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.advanced_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.advanced_toggle.setCheckable(True)
        right.addWidget(self.advanced_toggle)
        self.advanced = QWidget()
        advanced = QVBoxLayout(self.advanced)
        advanced.setContentsMargins(0, 0, 0, 0)
        self.sample = SelectBox()
        for label, value in [('采样率：自动', 0), ('44.1 kHz', 44100), ('48 kHz', 48000)]:
            self.sample.addItem(label, value)
        advanced.addWidget(self.sample)
        self.channels = SelectBox()
        for label, value in [('声道：自动', 0), ('单声道', 1), ('立体声', 2)]:
            self.channels.addItem(label, value)
        advanced.addWidget(self.channels)
        self.concurrency = SelectBox()
        for count in (1, 2, 3, 4):
            self.concurrency.addItem(f'同时转换：{count} 个', count)
        self.concurrency.setCurrentIndex(1)
        advanced.addWidget(self.concurrency)
        self.structure = QCheckBox('保留子文件夹结构')
        self.structure.setChecked(True)
        advanced.addWidget(self.structure)
        self.kgg_keys, self.kgg_keys_button = self.folder_field(
            advanced, '酷狗 KGG 密钥库', '自动查找本机酷狗密钥库', self.choose_kgg_keys)
        self.kgg_keys.setReadOnly(False)
        self.kgg_keys.setClearButtonEnabled(True)
        self.kgg_keys.setToolTip('可选择 KGMusicV3.db 或 kgg.key；清空后自动查找。')
        self.quality_note = QLabel('优先原音质：兼容时保留原码率和声道，不兼容时以 192 kbps 转换。指定采样率或声道会重新编码。')
        self.quality_note.setWordWrap(True)
        self.quality_note.setProperty('muted', True)
        advanced.addWidget(self.quality_note)
        right.addWidget(self.advanced)
        self.advanced.hide()
        self.advanced_toggle.toggled.connect(self.toggle_advanced)
        self.line(right)
        self.keep = QCheckBox('保留源文件')
        self.keep.setChecked(True)
        right.addWidget(self.keep)
        self.keep_note = QLabel('取消勾选后，仅将转换成功且校验通过的源文件移入回收站。')
        self.keep_note.setWordWrap(True)
        self.keep_note.setProperty('muted', True)
        right.addWidget(self.keep_note)
        self.keep.toggled.connect(self.keep_changed)
        self.keep_note.setMinimumHeight(50)
        right.addStretch(1)
        self.start_button = QPushButton('开始转换')
        self.start_button.setObjectName('primary')
        self.start_button.setMinimumHeight(49)
        self.start_button.clicked.connect(self.start)
        right.addWidget(self.start_button)
        self.cancel_button = QPushButton('取消转换')
        self.cancel_button.clicked.connect(self.cancel)
        right.addWidget(self.cancel_button)
        self.cancel_button.hide()
        body.addWidget(scroll, 3)
        self.format.currentTextChanged.connect(self.format_changed)

        footer = QFrame()
        footer.setObjectName('footer')
        foot = QHBoxLayout(footer)
        foot.setContentsMargins(27, 13, 25, 13)
        status_col = QVBoxLayout()
        status_col.setSpacing(3)
        self.summary = QLabel('尚未添加音频')
        self.summary.setWordWrap(True)
        self.hint = QLabel('输出 MP3 · 保留源文件')
        self.hint.setProperty('muted', True)
        status_col.addWidget(self.summary)
        status_col.addWidget(self.hint)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        status_col.addWidget(self.progress)
        self.progress.hide()
        foot.addLayout(status_col, 1)
        self.retry_button = QPushButton('重试未成功项')
        self.retry_button.clicked.connect(self.retry)
        foot.addWidget(self.retry_button)
        self.reconvert_button = QPushButton('重新转换选中项')
        self.reconvert_button.clicked.connect(self.reconvert_selected)
        foot.addWidget(self.reconvert_button)
        self.open_button = QPushButton('打开输出文件夹')
        self.open_button.clicked.connect(self.open_output)
        foot.addWidget(self.open_button)
        outer.addWidget(footer)
        self.lockable = [self.add_button, self.source_button, self.output_button, self.recursive,
                         self.format, self.quality, self.sample, self.channels, self.concurrency, self.structure, self.keep,
                         self.clear_button, self.drop]
        self.lockable.extend([self.kgg_keys, self.kgg_keys_button])

    def folder_field(self, parent, label, placeholder, callback):
        parent.addWidget(QLabel(label))
        row = QHBoxLayout()
        row.setSpacing(7)
        field = QLineEdit()
        field.setReadOnly(True)
        field.setPlaceholderText(placeholder)
        field.setMinimumWidth(0)
        button = QPushButton('浏览…')
        button.setFixedWidth(66)
        button.clicked.connect(callback)
        row.addWidget(field, 1)
        row.addWidget(button)
        parent.addLayout(row)
        return field, button

    def line(self, layout):
        line = QFrame()
        line.setObjectName('divider')
        line.setFixedHeight(1)
        layout.addSpacing(3)
        layout.addWidget(line)
        layout.addSpacing(3)

    def busy(self):
        return bool((self.worker and self.worker.isRunning()) or (self.scanner and self.scanner.isRunning()))

    def choose_kgg_keys(self):
        path, _ = QFileDialog.getOpenFileName(
            self, '选择原下载设备的酷狗密钥库', self.kgg_keys.text(),
            '酷狗密钥库 (KGMusicV3.db kgg.key);;数据库 (*.db);;密钥文件 (*.key)')
        if path:
            self.kgg_keys.setText(path)

    def choose_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, '添加音频文件', '', '音频文件 ('+' '.join('*'+e for e in sorted(INPUTS))+');;所有文件 (*)')
        if paths:
            self.scan([Path(p) for p in paths])

    def choose_source(self):
        path = QFileDialog.getExistingDirectory(self, '选择原音频文件夹', self.source.text())
        if path:
            self.source.setText(str(Path(path)))
            self.source.setToolTip(path)
            self.items.clear()
            self.refresh()
            self.scan([Path(path)])

    def rescan_source(self):
        if self.source.text() and not self.busy():
            self.items.clear()
            self.refresh()
            self.scan([Path(self.source.text())])

    def choose_output(self):
        path = QFileDialog.getExistingDirectory(self, '选择输出文件夹', self.output.text())
        if path:
            self.output.setText(str(Path(path)))
            self.output.setToolTip(path)
            # Exclude already-scanned files inside a nested output directory.
            if self.source.text():
                root, dest = Path(self.source.text()).resolve(), Path(path).resolve()
                if dest != root and dest.is_relative_to(root):
                    self.items = [item for item in self.items if not item.source.is_relative_to(dest)]
                    self.refresh()
            self.update_controls()

    def scan(self, paths):
        if self.busy() or not paths:
            return
        exclude = None
        if self.output.text():
            dest = Path(self.output.text()).resolve()
            if any(p.is_dir() and dest != p.resolve() and dest.is_relative_to(p.resolve()) for p in paths):
                exclude = dest
        self.scanner = Scanner(paths, self.recursive.isChecked(), exclude)
        self.scanner.ready.connect(self.scanned)
        self.scanner.failed.connect(lambda text: self.summary.setText(text))
        self.scanner.finished.connect(self.scan_finished)
        self.summary.setText('正在扫描音频文件…')
        self.scanner.start()
        self.update_controls()

    def scanned(self, items):
        existing = {item.source for item in self.items}
        self.items.extend(item for item in items if item.source not in existing)
        self.refresh()
        self.summary.setText(f'{len(self.items)} 个音频文件' if self.items else '未找到支持的音频文件')

    def scan_finished(self):
        self.update_controls()
        if self.closing:
            self.close()

    def change_filter(self, value):
        self.filter = value
        self.refresh()

    def refresh(self):
        selected = {row.data(0, Qt.ItemDataRole.UserRole) for row in self.tree.selectedItems()}
        self.tree.clear()
        self.rows = {}
        for index, item in enumerate(self.items):
            row = QTreeWidgetItem([item.source.name, item.source.suffix[1:].upper(), pretty_duration(item.duration),
                                  {'pending':'等待转换','running':'处理中','done':'完成','warn':'完成 · 请查看提示',
                                   'error':'失败','cancelled':'已取消'}[item.state]])
            row.setData(0, Qt.ItemDataRole.UserRole, index)
            row.setToolTip(0, str(item.source) + f'\n{item.size / 1048576:.2f} MB')
            row.setToolTip(3, item.detail)
            row.setSizeHint(0, QSize(0, 49))
            self.tree.addTopLevelItem(row)
            row.setSelected(index in selected)
            row.setHidden(self.filter == 'done' and item.state not in ('done', 'warn') or self.filter == 'error' and item.state not in ('error', 'cancelled'))
            self.rows[index] = row
            self.color_row(row, item.state)
        counts = [len(self.items), sum(i.state in ('done','warn') for i in self.items), sum(i.state in ('error','cancelled') for i in self.items)]
        for b, label, key, count in zip(self.tabs, ['全部','已完成','未成功'], ['all','done','error'], counts):
            b.setText(f'{label}  {count}')
            b.setChecked(self.filter == key)
        self.drop.setVisible(not self.items)
        self.tree.setMaximumHeight(82 if not self.items else 16777215)
        self.update_controls()

    def color_row(self, row, state):
        dark = QApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
        color = '#73edbb' if dark else '#147955'
        if state in ('error', 'warn', 'cancelled'):
            color = '#edbd7c' if dark else '#9d551b'
        elif state not in ('done', 'running'):
            color = '#a0aaa5' if dark else '#737e78'
        row.setForeground(3, QColor(color))

    def clear_items(self):
        if not self.busy():
            self.items.clear()
            self.refresh()
            self.summary.setText('尚未添加音频')

    def update_controls(self):
        busy = self.busy()
        if not hasattr(self, 'lockable'):
            return
        for widget in self.lockable:
            widget.setEnabled(not busy)
        if not busy:
            self.quality.setEnabled(self.format.currentText() not in ('FLAC', 'WAV'))
        self.start_button.setEnabled(not busy and bool(self.output.text()) and any(i.state == 'pending' for i in self.items))
        self.clear_button.setEnabled(not busy and bool(self.items))
        self.retry_button.setEnabled(not busy and bool(self.output.text()) and any(i.state in ('error','cancelled') for i in self.items))
        selected = [row.data(0, Qt.ItemDataRole.UserRole) for row in self.tree.selectedItems()]
        self.reconvert_button.setEnabled(not busy and bool(self.output.text()) and
                                         any(0 <= i < len(self.items) and self.items[i].state in ('done', 'warn')
                                             for i in selected))
        self.open_button.setEnabled(bool(self.output.text()))
        self.hint.setText('输出 '+self.format.currentText()+' · '+('保留源文件' if self.keep.isChecked() else '校验通过后移入回收站'))

    def toggle_advanced(self, expanded):
        self.advanced.setVisible(expanded)
        self.advanced_toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)

    def format_changed(self):
        lossless = self.format.currentText() in ('FLAC', 'WAV')
        self.quality.setEnabled(not lossless and not self.busy())
        self.quality_note.setText('无损输出不会恢复源文件已损失的音质。' if lossless else
                                  'Opus 自动使用 48 kHz；自动声道对 MP3/OGG/Opus 输出立体声。' if self.format.currentText() == 'Opus' else
                                  '优先原音质：兼容时保留原码率和声道，不兼容时以 192 kbps 转换。指定采样率或声道会重新编码。')
        self.update_controls()

    def keep_changed(self):
        self.keep_note.setText('取消勾选后，仅将转换成功且校验通过的源文件移入回收站。' if self.keep.isChecked() else
                              '失败或取消时保留源文件；无法移入回收站时也会保留，绝不永久删除。')
        self.update_controls()

    def start(self, checked=False, indices=None):
        if self.busy() or not self.output.text():
            return
        jobs = [(n, item) for n, item in enumerate(self.items)
                if item.state == 'pending' and (indices is None or n in indices)]
        if not jobs:
            return
        output = Path(self.output.text())
        try:
            output.mkdir(parents=True, exist_ok=True)
            import tempfile
            with tempfile.TemporaryFile(dir=output):
                pass
        except OSError as error:
            QMessageBox.warning(self, '无法写入输出文件夹', str(error))
            return
        options = Options(output, self.format.currentText(), self.quality.currentData() or 192,
                          self.sample.currentData(), self.channels.currentData(), self.keep.isChecked(),
                          self.structure.isChecked(), fast=self.quality.currentData() == 0,
                          kgg_keys=Path(self.kgg_keys.text().strip()) if self.kgg_keys.text().strip() else None)
        self.batch_count, self.completed_count = len(jobs), 0
        self.job_progress_values = {index: 0 for index, _ in jobs}
        self.worker = Converter(jobs, options, self.concurrency.currentData())
        self.worker.status.connect(self.job_progress)
        self.worker.result.connect(self.job_result)
        self.worker.finished.connect(self.batch_finished)
        self.progress.setValue(0)
        self.progress.show()
        self.cancel_button.setEnabled(True)
        self.cancel_button.setText('取消转换')
        self.cancel_button.show()
        self.start_button.setText('正在转换')
        self.worker.start()
        self.update_controls()

    def job_progress(self, index, status, progress):
        item = self.items[index]
        item.state = 'running'
        self.job_progress_values[index] = progress
        row = self.rows.get(index)
        if row:
            row.setText(3, f'{status} {progress}%')
            self.color_row(row, 'running')
        self.progress.setValue(int(sum(self.job_progress_values.values()) / self.batch_count * 10))
        running = sum(i.state == 'running' for _, i in self.worker.jobs)
        self.summary.setText(f'已完成 {self.completed_count} / {self.batch_count} · 正在处理 {running} 个')

    def job_result(self, index, result, error):
        item = self.items[index]
        self.completed_count += 1
        self.job_progress_values[index] = 100
        if result:
            item.state = 'warn' if result.warning else 'done'
            item.output, item.duration = result.path, result.duration
            item.detail = str(result.path) + ('\n源文件已移入回收站' if result.recycled else '\n源文件回收未确认' if result.warning else '\n源文件保留') + ('\n'+result.warning if result.warning else '')
        else:
            item.state = 'cancelled' if self.worker.cancel.is_set() else 'error'
            item.detail = explain_failure(error, Path(self.output.text()))
        self.refresh()
        self.progress.setValue(int(sum(self.job_progress_values.values()) / self.batch_count * 10))

    def batch_finished(self):
        if self.worker.cancel.is_set():
            for _, item in self.worker.jobs:
                if item.state == 'pending':
                    item.state = 'cancelled'
                    item.detail = '未开始，源文件保留'
        self.refresh()
        self.progress.hide()
        self.cancel_button.hide()
        self.start_button.setText('开始转换')
        success = sum(i.state in ('done','warn') for _, i in self.worker.jobs)
        failed = sum(i.state == 'error' for _, i in self.worker.jobs)
        cancelled = sum(i.state == 'cancelled' for _, i in self.worker.jobs)
        warnings = sum(i.state == 'warn' for _, i in self.worker.jobs)
        self.summary.setText(f'已完成 {success} · 失败 {failed} · 取消 {cancelled}' + (f' · {warnings} 项提示' if warnings else ''))
        failures = [(item.source.name, item.detail) for _, item in self.worker.jobs if item.state == 'error']
        if failures:
            details = '\n'.join(f'{name}：{reason.splitlines()[0]}' for name, reason in failures[:3])
            extra = f'\n另有 {len(failures)-3} 项失败' if len(failures) > 3 else ''
            QMessageBox.warning(self, f'{len(failures)} 个音频处理失败', details + extra + '\n\n选中失败任务并双击，或右键选择“查看详情”，可查看具体错误信息。')
        self.update_controls()
        if self.closing:
            self.close()

    def cancel(self):
        if self.worker and self.worker.isRunning():
            self.worker.cancel.set()
            self.cancel_button.setEnabled(False)
            self.cancel_button.setText('正在取消…')

    def retry(self):
        if self.busy():
            return
        for item in self.items:
            if item.state in ('error', 'cancelled'):
                item.state, item.detail = 'pending', ''
        self.refresh()
        self.start()

    def reconvert_selected(self):
        if self.busy():
            return
        selected = {row.data(0, Qt.ItemDataRole.UserRole) for row in self.tree.selectedItems()}
        changed = set()
        missing = []
        for index in selected:
            if 0 <= index < len(self.items) and self.items[index].state in ('done', 'warn'):
                if not self.items[index].source.is_file():
                    missing.append(self.items[index].source.name)
                    continue
                self.items[index].state = 'pending'
                self.items[index].detail = ''
                changed.add(index)
        if missing:
            QMessageBox.information(self, '源文件不可用',
                                    '以下源文件已移走或进入回收站，请恢复源文件后重新转换：\n' + '\n'.join(missing[:10]))
        if changed:
            self.refresh()
            self.start(indices=changed)

    def open_output(self):
        if self.output.text():
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.output.text()))

    def open_item(self, row, column=0):
        item = self.items[row.data(0, Qt.ItemDataRole.UserRole)]
        if item.output and item.output.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(item.output)))
        elif item.detail:
            QMessageBox.information(self, item.source.name, item.detail)

    def context_menu(self, position):
        row = self.tree.itemAt(position)
        if not row:
            return
        if not row.isSelected():
            self.tree.clearSelection()
            row.setSelected(True)
        index = row.data(0, Qt.ItemDataRole.UserRole)
        item = self.items[index]
        menu = QMenu(self)
        menu.addAction('打开源文件夹', lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(item.source.parent))))
        if item.output:
            menu.addAction('打开转换结果', lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(item.output))))
        if item.detail:
            menu.addAction('查看详情', lambda: QMessageBox.information(self, '任务详情', item.detail))
        if not self.busy():
            menu.addSeparator()
            if item.state in ('done', 'warn'):
                menu.addAction('重新转换', self.reconvert_selected)
            menu.addAction('从列表移除选中项', self.remove_selected)
        menu.exec(self.tree.viewport().mapToGlobal(position))

    def remove_selected(self):
        selected = {row.data(0, Qt.ItemDataRole.UserRole) for row in self.tree.selectedItems()}
        self.items = [item for index, item in enumerate(self.items) if index not in selected]
        self.refresh()

    def closeEvent(self, event):
        if self.busy():
            self.closing = True
            if self.worker and self.worker.isRunning():
                self.cancel()
            if self.scanner and self.scanner.isRunning():
                self.scanner.cancel.set()
            self.summary.setText('正在停止任务，完成清理后关闭…')
            event.ignore()
        else:
            event.accept()

    def apply_theme(self):
        dark = QApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
        bg, panel, ink, muted, line, accent, soft = ('#1d2123','#1d2123','#e8eeeb','#98a49d','#343c38','#72efba','#293d33') if dark else ('#ffffff','#f6f9f7','#23302a','#6b7c72','#dfe7e2','#36ca90','#e6f6ed')
        self.theme_label.setText('主题：跟随系统 · '+('深色' if dark else '浅色'))
        check = (Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / 'assets' / 'check.svg').as_posix()
        self.setStyleSheet(f'''
            QWidget {{background:{bg}; color:{ink}; font-family:'Microsoft YaHei UI'; font-size:13px;}}
            QMainWindow {{background:{bg};}}
            QFrame#header {{border-bottom:1px solid {line};}}
            QLabel#brand {{font-family:'Segoe UI';font-size:22px;font-weight:500;}}
            QLabel#sectionTitle {{font-size:25px;font-weight:600;}}
            QLabel[muted="true"] {{color:{muted};font-size:11px;}}
            QWidget#side {{background:{panel};border-left:1px solid {line};}}
            QWidget#side QLabel,QWidget#side QCheckBox,QWidget#side QToolButton {{background:transparent;}}
            QFrame#divider {{background:{line};}}
            QFrame#footer {{border-top:1px solid {line};}}
            QPushButton {{border:1px solid {line};border-radius:6px;padding:9px 12px;background:{bg};}}
            QPushButton:hover {{background:{soft};border-color:{accent};}}
            QPushButton:disabled {{color:{muted};background:{panel};border-color:{line};}}
            QPushButton#primary {{background:{accent};border-color:{accent};color:#123b29;font-size:17px;font-weight:600;}}
            QPushButton#primary:hover {{background:#85f3c5;}}
            QPushButton#primary:disabled {{background:{soft};border-color:{line};color:{muted};}}
            QPushButton#quiet,QPushButton#tab {{border:0;background:transparent;color:{muted};font-size:12px;padding:7px 8px;}}
            QPushButton#tab:checked {{color:{accent if dark else '#147953'};border-bottom:2px solid {accent};border-radius:0;}}
            QLineEdit,QComboBox {{padding:10px 8px;background:{bg};border:1px solid {line};border-radius:6px;}}
            QLineEdit:focus,QComboBox:focus {{border:1px solid {accent};}}
            QComboBox:disabled {{color:{muted};}}
            QComboBox::drop-down {{width:25px;border:0;}}
            QComboBox QAbstractItemView {{background:{panel};selection-background-color:{soft};selection-color:{ink};border:1px solid {line};}}
            QCheckBox {{spacing:8px;}}
            QCheckBox::indicator {{width:16px;height:16px;}}
            QCheckBox::indicator:unchecked {{border:1px solid {muted};border-radius:3px;background:{bg};}}
            QCheckBox::indicator:checked {{border:1px solid {accent};border-radius:3px;background:{accent};image:url("{check}");}}
            QToolButton {{border:0;text-align:left;padding:6px 0;color:{muted};}}
            QTreeWidget {{border:0;outline:0;background:{bg};alternate-background-color:{panel};}}
            QTreeWidget::item {{border-bottom:1px solid {line};padding:6px;}}
            QTreeWidget::item:selected {{background:{soft};color:{ink};}}
            QTreeWidget::item:hover {{background:{panel};}}
            QHeaderView::section {{background:{panel};color:{muted};border:0;border-bottom:1px solid {line};padding:10px 6px;font-size:12px;}}
            QFrame#dropArea {{border:1px dashed {line};border-radius:7px;background:{bg};}}
            QFrame#dropArea QLabel {{border:0;background:transparent;color:{muted};}}
            QLabel#dropTitle {{font-size:17px;}}
            QProgressBar {{border:0;background:{line};border-radius:2px;}}
            QProgressBar::chunk {{background:{accent};border-radius:2px;}}
            QScrollArea {{border:0;}}
            QScrollBar:vertical {{background:{panel};width:9px;margin:0;}}
            QScrollBar::handle:vertical {{background:{line};border-radius:4px;min-height:30px;}}
            QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {{height:0;}}
            QMenu {{background:{panel};border:1px solid {line};padding:5px;}}
            QMenu::item {{padding:8px 18px;}}
            QMenu::item:selected {{background:{soft};}}
        ''')
        if hasattr(self, 'rows'):
            for index, row in self.rows.items():
                self.color_row(row, self.items[index].state)


def main():
    if '--self-check' in sys.argv:
        import json
        from verify import run_checks
        report_path = Path(sys.argv[sys.argv.index('--self-check')+1])
        try:
            results = run_checks()
            if '--check-xm' in sys.argv:
                source = Path(sys.argv[sys.argv.index('--check-xm')+1])
                result = convert(source, None, Options(report_path.parent / 'packed-xm-test', bitrate=128), threading.Event())
                results.append('Packed XM conversion passed: '+str(result.path))
            report_path.write_text(json.dumps({'ok':True, 'checks':results}, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception:
            import traceback
            report_path.write_text(json.dumps({'ok':False, 'error':traceback.format_exc()}, ensure_ascii=False), encoding='utf-8')
            raise
        return
    app = QApplication(sys.argv)
    app.setApplicationName('AudioConvert')
    app.setOrganizationName('AudioConvert')
    app.setApplicationVersion(APP_VERSION)
    app.setStyle('Fusion')
    app.setFont(QFont('Microsoft YaHei UI', 10))
    window = Window()
    window.show()
    # Internal verification flags never populate fake rows or perform conversions.
    if '--screenshot' in sys.argv:
        index = sys.argv.index('--screenshot')
        path = sys.argv[index+1]
        QTimer.singleShot(600, lambda: window.grab().save(path))
        QTimer.singleShot(1000, app.quit)
    sys.exit(app.exec())


if __name__ == '__main__':
    try:
        main()
    except Exception:
        log.exception('Unhandled startup error')
        if QApplication.instance():
            QMessageBox.critical(None, 'AudioConvert 启动失败', '请查看日志：\n'+str(LOG_DIR / 'AudioConvert.log'))
        raise
