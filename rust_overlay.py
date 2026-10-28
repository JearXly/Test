"""Single-file external overlay and experimental read-only Rust layout reader.
Install: py -m pip install PySide6==6.10.2
Run: py rust_overlay.py
Supplied offsets are NOT verified for build 26140533. No anti-cheat bypass.
"""
import ctypes as C
from ctypes import wintypes as W
import math
import os
import struct
import sys
import time
from PySide6.QtCore import Qt, QTimer, QRectF, QThread, Signal, QSettings
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QFormLayout, QLabel, QPushButton, QComboBox, QLineEdit, QSpinBox, QCheckBox,
    QColorDialog, QGroupBox, QMessageBox)

DEFAULTS = dict(fps=60, max_distance=300, color='#45ffaa', line_width=2,
                font_size=12, boxes=True, names=True, distance=True, health=True,
                snaplines=False, crosshair=False, stale_seconds=1.0)

class ReadFailure(RuntimeError):
    pass


def project(matrix, point, width, height):
    # Original supplied routine transposes D3DXMATRIX, then uses its rows.
    # Equivalent indexing into the original row-major 16-float array below.
    x, y, z = point
    w = matrix[3]*x + matrix[7]*y + matrix[11]*z + matrix[15]
    if w < .098:
        return None
    px = matrix[0]*x + matrix[4]*y + matrix[8]*z + matrix[12]
    py = matrix[1]*x + matrix[5]*y + matrix[9]*z + matrix[13]
    return width/2*(1+px/w), height/2*(1-py/w)  # screen Y points downward


class WindowsMemory:
    def __init__(self, pid):
        if os.name != 'nt':
            raise RuntimeError('Live memory reads require 64-bit Windows Python.')
        if C.sizeof(C.c_void_p) != 8:
            raise RuntimeError('Install 64-bit Python.')
        self.api = C.WinDLL('kernel32', use_last_error=True)
        self.api.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
        self.api.OpenProcess.restype = W.HANDLE
        self.api.ReadProcessMemory.argtypes = [W.HANDLE, C.c_void_p, C.c_void_p,
                                               C.c_size_t, C.POINTER(C.c_size_t)]
        self.api.ReadProcessMemory.restype = W.BOOL
        self.api.CloseHandle.argtypes = [W.HANDLE]
        self.api.CloseHandle.restype = W.BOOL
        # PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ. No write rights.
        self.handle = self.api.OpenProcess(0x1000 | 0x10, False, pid)
        if not self.handle:
            raise ReadFailure(f'OpenProcess failed: Windows error {C.get_last_error()}. '
                              'Access may be protected; no bypass is implemented.')

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None

    def read(self, address, size):
        if getattr(self, 'cancelled', lambda: False)():
            raise ReadFailure('Reader stopped')
        if not 0x10000 <= address <= 0x7FFFFFFFFFFF or not 0 < size <= 65536:
            raise ReadFailure(f'Invalid address/size: {address:#x}/{size}')
        buffer = C.create_string_buffer(size)
        count = C.c_size_t()
        if not self.api.ReadProcessMemory(self.handle, address, buffer, size, C.byref(count)) or count.value != size:
            raise ReadFailure(f'ReadProcessMemory failed at {address:#x}: '
                              f'Windows error {C.get_last_error()}, bytes={count.value}/{size}')
        return buffer.raw

    def unpack(self, address, fmt):
        return struct.unpack('<'+fmt, self.read(address, struct.calcsize('<'+fmt)))

    def ptr(self, address):
        return self.unpack(address, 'Q')[0]

    def name(self, address):
        length = self.unpack(address+0x10, 'i')[0]
        if not 0 <= length <= 256:
            raise ReadFailure('Invalid managed string length')
        return self.read(address+0x14, length*2).decode('utf-16-le', errors='replace') if length else ''


class SuppliedLayout:
    def __init__(self, memory, module_base, offset, width, height):
        self.m = memory
        self.root = module_base + offset
        self.width, self.height = width, height

    def objects(self):
        m = self.m
        manager = m.ptr(self.root)
        last_node, node = m.unpack(manager, 'QQ')
        last_object = m.ptr(last_node+0x10) if last_node else 0
        visited = set()
        for _ in range(20000):
            if not node or node in visited:
                return
            visited.add(node)
            next_node, obj = m.unpack(node+8, 'QQ')
            if obj:
                yield obj, m.unpack(obj+0x4C, 'H')[0]
            if obj == last_object:
                return
            node = next_node
        raise ReadFailure('Object-list limit exceeded; layout may be incompatible')

    def snapshot(self):
        m = self.m
        camera, players = None, []
        for obj, tag in self.objects():
            if tag not in (5, 6):
                continue
            try:
                component = m.ptr(obj+0x28)
                transform = m.ptr(component+8)
                position = m.unpack(transform+0x40, 'fff')
                if not all(math.isfinite(v) and abs(v) < 1e7 for v in position):
                    continue
                if tag == 5:
                    camera_ptr = m.ptr(component+0x18)
                    matrix = m.unpack(camera_ptr+0xB8, '16f')
                    if all(math.isfinite(v) for v in matrix):
                        camera = position, matrix
                else:
                    entity = m.ptr(m.ptr(component+0x18)+0x20)
                    if m.unpack(entity+0x148, 'B')[0]:
                        continue
                    health = m.unpack(entity+0x14C, 'f')[0]
                    if not math.isfinite(health) or not 0 < health <= 1000:
                        continue
                    name = m.name(m.ptr(entity+0x208))
                    players.append((position, name, health))
            except ReadFailure:
                # Entity destruction between reads is normal; global failures
                # above still propagate and generate diagnostics.
                continue
        if camera is None:
            raise ReadFailure('No valid tagged camera found; supplied layout is unverified')
        origin, matrix = camera
        result = []
        for position, name, health in players:
            distance = math.dist(origin, position)
            if distance < 1:
                continue
            feet = project(matrix, position, self.width, self.height)
            head = project(matrix, (position[0], position[1]+1.8, position[2]), self.width, self.height)
            if not feet or not head:
                continue
            h = feet[1]-head[1]
            if h <= 1 or h > self.height*4:
                continue
            w = h*.45
            result.append(dict(x=head[0]-w/2, y=head[1], w=w, h=h,
                               name=name, health=health, distance=distance))
        return result


class PROCESSENTRY32W(C.Structure):
    _fields_ = [('dwSize', W.DWORD), ('cntUsage', W.DWORD), ('th32ProcessID', W.DWORD),
                ('th32DefaultHeapID', C.c_size_t), ('th32ModuleID', W.DWORD),
                ('cntThreads', W.DWORD), ('th32ParentProcessID', W.DWORD),
                ('pcPriClassBase', W.LONG), ('dwFlags', W.DWORD), ('szExeFile', W.WCHAR*260)]


class MODULEENTRY32W(C.Structure):
    _fields_ = [('dwSize', W.DWORD), ('th32ModuleID', W.DWORD), ('th32ProcessID', W.DWORD),
                ('GlblcntUsage', W.DWORD), ('ProccntUsage', W.DWORD),
                ('modBaseAddr', C.c_void_p), ('modBaseSize', W.DWORD), ('hModule', W.HMODULE),
                ('szModule', W.WCHAR*256), ('szExePath', W.WCHAR*260)]


def enumerate_windows(pid=None):
    if os.name != 'nt' or C.sizeof(C.c_void_p) != 8:
        raise RuntimeError('Live mode requires 64-bit Python on Windows.')
    api = C.WinDLL('kernel32', use_last_error=True)
    api.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
    api.CreateToolhelp32Snapshot.restype = W.HANDLE
    api.CloseHandle.argtypes = [W.HANDLE]
    entry_type = PROCESSENTRY32W if pid is None else MODULEENTRY32W
    prefix = 'Process32' if pid is None else 'Module32'
    first, next_entry = getattr(api, prefix+'FirstW'), getattr(api, prefix+'NextW')
    for fn in (first, next_entry):
        fn.argtypes = [W.HANDLE, C.POINTER(entry_type)]
        fn.restype = W.BOOL
    handle = api.CreateToolhelp32Snapshot(2 if pid is None else 0x18, pid or 0)
    if handle == C.c_void_p(-1).value:
        raise ReadFailure(f'Windows enumeration failed: error {C.get_last_error()}')
    entry = entry_type()
    entry.dwSize = C.sizeof(entry)
    result = []
    try:
        ok = first(handle, C.byref(entry))
        while ok:
            if pid is None:
                result.append((entry.szExeFile, int(entry.th32ProcessID)))
            else:
                result.append((entry.szModule, int(entry.modBaseAddr or 0), int(entry.modBaseSize)))
            ok = next_entry(handle, C.byref(entry))
        error = C.get_last_error()
        if error != 18:  # ERROR_NO_MORE_FILES
            raise ReadFailure(f'Windows enumeration stopped: error {error}')
    finally:
        api.CloseHandle(handle)
    return result


class Reader(QThread):
    snapshot_ready = Signal(list)
    status_changed = Signal(str)

    def __init__(self, pid, base, offset, width, height):
        super().__init__()
        self.args = pid, base, offset, width, height

    def run(self):
        memory = None
        last_status = 0
        try:
            pid, base, offset, width, height = self.args
            memory = WindowsMemory(pid)
            memory.cancelled = self.isInterruptionRequested
            if isinstance(base, list):
                layout = None
                failures = []
                for name, candidate in base:
                    if self.isInterruptionRequested():
                        return
                    trial = SuppliedLayout(memory, candidate, offset, width, height)
                    try:
                        trial.snapshot()
                        layout = trial
                        self.status_changed.emit(f'Supplied camera layout readable in {name}; '
                                                 'game-build compatibility is still unverified.')
                        break
                    except ReadFailure as exc:
                        failures.append(f'{name}: {exc}')
                if layout is None:
                    raise ReadFailure('Supplied layout failed in every candidate module. '
                                      'Matching offsets are still required. '+ '; '.join(failures[:3]))
            else:
                layout = SuppliedLayout(memory, base, offset, width, height)
            while not self.isInterruptionRequested():
                started = time.monotonic()
                try:
                    entities = layout.snapshot()
                    self.snapshot_ready.emit(entities)
                    status = (f'Read snapshot: {len(entities)} projected players. '
                              'Build compatibility remains unverified.')
                except ReadFailure as exc:
                    self.snapshot_ready.emit([])
                    status = 'Read failed: '+str(exc)
                if started-last_status >= 1:
                    self.status_changed.emit(status)
                    last_status = started
                self.msleep(50)
        except (ReadFailure, RuntimeError, OSError) as exc:
            self.snapshot_ready.emit([])
            self.status_changed.emit(str(exc))
        finally:
            if memory:
                memory.close()


class Overlay(QWidget):
    def __init__(self):
        super().__init__()
        self.config = dict(DEFAULTS)
        self.entities = []
        self.demo = False
        self.received_at = 0
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool |
                            Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setGeometry(QApplication.primaryScreen().geometry())
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(16)

    def tick(self):
        if self.demo:
            self.accept_snapshot([dict(x=self.width()/2+math.sin(time.monotonic())*150,
                y=self.height()/3, w=80, h=180, name='DEMO ENTITY', distance=42, health=75)])
        self.update()

    def accept_snapshot(self, entities):
        self.entities = entities
        self.received_at = time.monotonic()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = self.config
        color = QColor(c["color"])
        p.setFont(QFont("Segoe UI", c["font_size"]))
        p.setPen(QPen(color, c["line_width"]))
        p.drawText(20, 30, "DEMO — no Rust connection" if self.demo else "TELEMETRY OVERLAY")
        if c["crosshair"]:
            x, y = self.width() // 2, self.height() // 2
            p.drawLine(x - 6, y, x + 6, y)
            p.drawLine(x, y - 6, x, y + 6)
        if time.monotonic() - self.received_at > c["stale_seconds"]:
            p.drawText(20, 55, "Waiting for screen-space telemetry")
            return
        for e in self.entities:
            if e["distance"] > c["max_distance"]:
                continue
            x, y, w, h = (e[k] for k in ("x", "y", "w", "h"))
            p.setPen(QPen(color, c["line_width"]))
            if c["boxes"]:
                p.drawRect(QRectF(x, y, w, h))
            label = []
            if c["names"]:
                label.append(e["name"])
            if c["distance"]:
                label.append(f'{e["distance"]:.0f} m')
            p.drawText(QRectF(x - 100, y - 25, w + 200, 24), Qt.AlignCenter, " · ".join(label))
            if c["health"]:
                fraction = max(0, min(1, e["health"] / 100))
                p.fillRect(QRectF(x - 8, y, 4, h), QColor("#444444"))
                p.fillRect(QRectF(x - 8, y + h * (1 - fraction), 4, h * fraction), color)
            if c["snaplines"]:
                p.drawLine(int(self.width() / 2), self.height(), int(x + w / 2), int(y + h))


class Menu(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Rust overlay — experimental layout')
        self.resize(560, 680)
        self.settings = QSettings('ExternalOverlayPrototype', 'RustOverlay')
        self.overlay = Overlay()
        self.reader = None
        outer = QVBoxLayout(self)
        warning = QLabel('Supplied offsets are unverified for Rust build 26140533. '
                         'Read-only access may be denied. This does not bypass anti-cheat.')
        warning.setWordWrap(True)
        outer.addWidget(warning)
        connection = QGroupBox('Read-only connection')
        form = QFormLayout(connection)
        self.process = QComboBox()
        self.modules = QComboBox()
        self.process.currentIndexChanged.connect(self.refresh_modules)
        refresh = QPushButton('Find Rust process')
        refresh.clicked.connect(self.refresh_processes)
        row = QHBoxLayout()
        row.addWidget(self.process); row.addWidget(refresh)
        form.addRow('Process', row)
        form.addRow('Module owning GOM offset', self.modules)
        note = QLabel('Auto checks the supplied layout against loaded modules using read-only access. '
                      'It cannot repair outdated offsets. Manual module selection is also available.')
        note.setWordWrap(True); form.addRow(note)
        self.offset = QLineEdit('0x125D5C0')
        form.addRow('Supplied GOM offset', self.offset)
        outer.addWidget(connection)
        controls = QGroupBox('Display options — apply immediately')
        options = QFormLayout(controls)
        self.checks = {}
        for key, label in [('boxes','Player boxes'), ('names','Names'), ('distance','Distance'),
                           ('health','Health bars'), ('snaplines','Snaplines'), ('crosshair','Crosshair')]:
            control = QCheckBox(label)
            control.setChecked(self.settings.value(key, DEFAULTS[key], type=bool))
            self.checks[key] = control
            control.toggled.connect(self.apply_options)
            options.addRow(control)
        self.spins = {}
        for key, label, low, high in [('max_distance','Maximum distance (m)',1,5000),
                                     ('line_width','Line width',1,10), ('font_size','Font size',8,48),
                                     ('fps','Overlay FPS',10,144)]:
            control = QSpinBox(); control.setRange(low,high)
            try: control.setValue(int(self.settings.value(key, DEFAULTS[key])))
            except (TypeError,ValueError): control.setValue(DEFAULTS[key])
            self.spins[key] = control
            control.valueChanged.connect(self.apply_options)
            options.addRow(label, control)
        self.color = str(self.settings.value('color', DEFAULTS['color']))
        if not QColor(self.color).isValid(): self.color = DEFAULTS['color']
        choose = QPushButton('Choose overlay color')
        choose.clicked.connect(self.choose_color); options.addRow(choose)
        outer.addWidget(controls)
        buttons = QHBoxLayout()
        demo = QPushButton('Start demo')
        demo.clicked.connect(self.start_demo)
        self.live = QPushButton('Start read-only reader')
        self.live.clicked.connect(self.start_reader)
        stop = QPushButton('Stop / clear')
        stop.clicked.connect(self.stop)
        for button in (demo,self.live,stop): buttons.addWidget(button)
        outer.addLayout(buttons)
        self.status = QLabel('Stopped. Demo works without Rust. Live mode requires matching offsets.')
        self.status.setWordWrap(True); self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        outer.addWidget(self.status)
        exit_button = QPushButton('Exit')
        exit_button.clicked.connect(self.close); outer.addWidget(exit_button)
        self.apply_options()
        self.refresh_processes()

    def apply_options(self):
        if not hasattr(self, 'spins') or not hasattr(self, 'color'):
            return
        config = {**DEFAULTS, **{k:c.isChecked() for k,c in self.checks.items()},
                  **{k:c.value() for k,c in self.spins.items()}, 'color':self.color}
        self.overlay.config = config
        self.overlay.timer.setInterval(round(1000/config['fps']))
        for key,value in config.items(): self.settings.setValue(key,value)
        self.overlay.update()

    def choose_color(self):
        color = QColorDialog.getColor(QColor(self.color), self)
        if color.isValid(): self.color = color.name(); self.apply_options()

    def refresh_processes(self):
        self.process.blockSignals(True)
        self.process.clear()
        try:
            for name,pid in enumerate_windows():
                if name.casefold() == 'rustclient.exe': self.process.addItem(f'{name} (PID {pid})',pid)
            if self.process.count() == 0: self.status.setText('RustClient.exe not found. Start Rust or use demo.')
        except (RuntimeError,ReadFailure,OSError) as exc:
            self.status.setText(str(exc)+' Demo remains available.')
        self.process.blockSignals(False)
        self.refresh_modules()

    def refresh_modules(self):
        self.modules.clear()
        pid = self.process.currentData()
        if pid is None: return
        try:
            modules = enumerate_windows(pid)
            self.modules.addItem('Auto — check supplied layout', modules)
            for name,base,size in modules:
                self.modules.addItem(f'{name} — {base:#x}', (base,size))
        except (RuntimeError,ReadFailure,OSError) as exc:
            self.status.setText(str(exc))

    def start_demo(self):
        self.stop()
        self.overlay.demo = True
        self.overlay.show()
        self.status.setText('DEMO: simulated entity only; no game connection.')

    def start_reader(self):
        self.stop()
        try:
            pid = self.process.currentData()
            module = self.modules.currentData()
            if pid is None or module is None:
                raise ValueError('Find Rust before starting the reader.')
            offset = int(self.offset.text().strip(),0)
            if offset < 0:
                raise ValueError('GOM offset must be nonnegative.')
            if isinstance(module,list):
                base = [(name,address) for name,address,size in module if offset <= size-8]
                if not base:
                    raise ValueError('No loaded module contains the supplied GOM offset. Layout is incompatible.')
            else:
                base,size = module
                if not offset <= size-8:
                    raise ValueError('GOM offset is outside the selected module. Check the source/layout.')
            self.reader = Reader(pid,base,offset,self.overlay.width(),self.overlay.height())
            worker = self.reader
            self.reader.snapshot_ready.connect(
                lambda entities: self.overlay.accept_snapshot(entities) if self.reader is worker else None)
            self.reader.status_changed.connect(
                lambda text: self.status.setText(text) if self.reader is worker else None)
            self.process.setEnabled(False); self.modules.setEnabled(False); self.offset.setEnabled(False)
            self.overlay.show()
            self.status.setText('Opening read-only access with the unverified supplied profile…')
            self.reader.start()
        except (ValueError,TypeError) as exc:
            self.status.setText(str(exc))

    def stop(self):
        if self.reader is not None:
            self.reader.requestInterruption()
            self.reader.wait()  # read() checks cancellation, including during object traversal
            # Drop any queued snapshots from the completed worker before clearing.
            self.reader.snapshot_ready.disconnect()
            self.reader.status_changed.disconnect()
            self.reader.deleteLater()
            self.reader = None
        self.overlay.demo = False
        self.overlay.entities = []
        self.overlay.hide()
        self.process.setEnabled(True); self.modules.setEnabled(True); self.offset.setEnabled(True)
        self.status.setText('Stopped. Overlay cleared.')

    def closeEvent(self, event):
        self.stop()
        self.overlay.close()
        self.settings.sync()
        event.accept()


def main():
    app = QApplication(sys.argv)
    menu = Menu()
    menu.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
