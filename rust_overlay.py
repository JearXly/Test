"""Single-file external overlay and experimental read-only Rust layout reader.
Install: py -m pip install PySide6==6.10.2
Run: py rust_overlay.py
Supplied offsets are NOT verified for build 26140533. No anti-cheat bypass.
"""
import ctypes as C
from ctypes import wintypes as W
import math
import csv
import io
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import platform
import struct
import subprocess
import sys
import time
from PySide6.QtCore import Qt, QTimer, QRectF, QThread, Signal, QSettings, QObject
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QFormLayout, QLabel, QPushButton, QComboBox, QLineEdit, QSpinBox, QCheckBox,
    QColorDialog, QGroupBox, QMessageBox, QPlainTextEdit)

VERSION = '2026.10.28-debug1'
LOG = logging.getLogger('rust_overlay')
LOG.setLevel(logging.DEBUG)
LOG_PATH = None


def configure_logging(directory=None):
    global LOG_PATH
    directory = Path(directory) if directory else Path(
        os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'RustOverlay' / 'logs'
    directory.mkdir(parents=True, exist_ok=True)
    LOG_PATH = directory / 'rust-overlay-debug.log'
    handler = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s [%(threadName)s] %(message)s'))
    LOG.addHandler(handler)
    LOG.info('SESSION START version=%s platform=%s Python=%s pointer_bits=%s',
             VERSION, platform.platform(), platform.python_version(), C.sizeof(C.c_void_p)*8)
    LOG.info('Debug logs contain API errors, module names/addresses and counters; no memory payloads or player names.')
    if os.name == 'nt':
        try:
            shell = C.WinDLL('shell32', use_last_error=True)
            shell.IsUserAnAdmin.restype = W.BOOL
            LOG.info('Windows elevated=%s', bool(shell.IsUserAnAdmin()))
        except Exception:
            LOG.exception('Could not query elevation status')
    return LOG_PATH


def api_failure(api, error, **context):
    explanation = C.FormatError(error).strip() if os.name == 'nt' else str(error)
    message = f'{api} failed: Windows error {error} ({explanation}); '+repr(context)
    if error == 5:
        message += ' Access denied; this is an API permission failure, not evidence about offset validity.'
    LOG.error(message)
    return ReadFailure(message)


class LogBridge(QObject):
    line = Signal(str)


class MenuLogHandler(logging.Handler):
    def __init__(self, bridge):
        super().__init__()
        self.bridge = bridge
        self.next_read_line = 0
        self.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s', datefmt='%H:%M:%S'))

    def emit(self, record):
        try:
            if record.levelno == logging.DEBUG and record.msg.startswith('ReadProcessMemory pid='):
                now = time.monotonic()
                if now < self.next_read_line:
                    return
                self.next_read_line = now+.1  # full trace remains on disk; bound GUI traffic
            self.bridge.line.emit(self.format(record))
        except RuntimeError:
            pass

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
        self.pid = pid
        self.stage = 'OpenProcess'
        self.read_calls = 0
        self.read_failures = 0
        self.trace_reads = False
        # PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ. No write rights.
        LOG.info('OpenProcess pid=%s requested_access=0x1010 (query-limited + VM-read)',pid)
        self.handle = self.api.OpenProcess(0x1000 | 0x10, False, pid)
        if not self.handle:
            raise api_failure('OpenProcess', C.get_last_error(),pid=pid,access='0x1010')
        LOG.info('OpenProcess succeeded pid=%s',pid)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None
            LOG.info('Process handle closed pid=%s reads=%s failed_reads=%s',
                     self.pid,self.read_calls,self.read_failures)

    def read(self, address, size):
        if getattr(self, 'cancelled', lambda: False)():
            raise ReadFailure('Reader stopped')
        if not 0x10000 <= address <= 0x7FFFFFFFFFFF or not 0 < size <= 65536:
            raise ReadFailure(f'Invalid address/size: {address:#x}/{size}')
        buffer = C.create_string_buffer(size)
        count = C.c_size_t()
        self.read_calls += 1
        if self.trace_reads:
            LOG.debug('ReadProcessMemory pid=%s stage=%s address=%#x bytes=%s',self.pid,self.stage,address,size)
        if not self.api.ReadProcessMemory(self.handle, address, buffer, size, C.byref(count)) or count.value != size:
            error = C.get_last_error()
            self.read_failures += 1
            message = (f'ReadProcessMemory pid={self.pid} stage={self.stage} address={address:#x} '
                       f'error={error} bytes={count.value}/{size}')
            if self.read_failures <= 10 or self.read_failures % 100 == 0 or self.trace_reads:
                LOG.error(message)
            raise ReadFailure(message)
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
        self.last_debug = 0
        self.stats = {}

    def objects(self):
        m = self.m
        m.stage = 'GOM root pointer'
        manager = m.ptr(self.root)
        m.stage = 'GOM tagged list'
        last_node, node = m.unpack(manager, 'QQ')
        last_object = m.ptr(last_node+0x10) if last_node else 0
        visited = set()
        for _ in range(20000):
            if not node or node in visited:
                return
            visited.add(node)
            self.stats['objects'] = len(visited)
            m.stage = 'tagged-list node'
            next_node, obj = m.unpack(node+8, 'QQ')
            if obj:
                yield obj, m.unpack(obj+0x4C, 'H')[0]
            if obj == last_object:
                return
            node = next_node
        raise ReadFailure('Object-list limit exceeded; layout may be incompatible')

    def snapshot(self):
        m = self.m
        self.stats = dict(objects=0,camera_tags=0,player_tags=0,invalid_reads=0,
                          invalid_positions=0,dead=0,invalid_health=0,near_camera=0,
                          behind_camera=0,invalid_box=0,projected=0)
        camera, players = None, []
        for obj, tag in self.objects():
            if tag not in (5, 6):
                continue
            try:
                self.stats['camera_tags' if tag == 5 else 'player_tags'] += 1
                m.stage = 'camera component/transform' if tag == 5 else 'player component/transform'
                component = m.ptr(obj+0x28)
                transform = m.ptr(component+8)
                position = m.unpack(transform+0x40, 'fff')
                if not all(math.isfinite(v) and abs(v) < 1e7 for v in position):
                    self.stats['invalid_positions'] += 1
                    continue
                if tag == 5:
                    m.stage = 'camera view matrix'
                    camera_ptr = m.ptr(component+0x18)
                    matrix = m.unpack(camera_ptr+0xB8, '16f')
                    if all(math.isfinite(v) for v in matrix):
                        camera = position, matrix
                else:
                    m.stage = 'player entity/health/name'
                    entity = m.ptr(m.ptr(component+0x18)+0x20)
                    if m.unpack(entity+0x148, 'B')[0]:
                        self.stats['dead'] += 1
                        continue
                    health = m.unpack(entity+0x14C, 'f')[0]
                    if not math.isfinite(health) or not 0 < health <= 1000:
                        self.stats['invalid_health'] += 1
                        continue
                    name = m.name(m.ptr(entity+0x208))
                    players.append((position, name, health))
            except ReadFailure:
                self.stats['invalid_reads'] += 1
                # Entity destruction between reads is normal; global failures
                # above still propagate and generate diagnostics.
                continue
        if camera is None:
            raise ReadFailure(f'No valid tagged camera found. root={self.root:#x}; counters={self.stats}')
        origin, matrix = camera
        result = []
        for position, name, health in players:
            distance = math.dist(origin, position)
            if distance < 1:
                self.stats['near_camera'] += 1
                continue
            feet = project(matrix, position, self.width, self.height)
            head = project(matrix, (position[0], position[1]+1.8, position[2]), self.width, self.height)
            if not feet or not head:
                self.stats['behind_camera'] += 1
                continue
            h = feet[1]-head[1]
            if h <= 1 or h > self.height*4:
                self.stats['invalid_box'] += 1
                continue
            w = h*.45
            result.append(dict(x=head[0]-w/2, y=head[1], w=w, h=h,
                               name=name, health=health, distance=distance))
        self.stats['projected'] = len(result)
        if time.monotonic()-self.last_debug >= 2:
            LOG.debug('Layout root=%#x counters=%s',self.root,self.stats)
            self.last_debug = time.monotonic()
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


def last_error():
    return C.get_last_error() if os.name == 'nt' else 0


def enumerate_toolhelp(api, pid=None):
    """Separate discovery stages so errors identify process versus module access."""
    flags = 2 if pid is None else 0x18
    stage = 'process list' if pid is None else f'module list for PID {pid}'
    LOG.info('CreateToolhelp32Snapshot stage=%s flags=%#x pid=%s',stage,flags,pid or 0)
    api.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
    api.CreateToolhelp32Snapshot.restype = W.HANDLE
    api.CloseHandle.argtypes = [W.HANDLE]
    api.CloseHandle.restype = W.BOOL
    entry_type = PROCESSENTRY32W if pid is None else MODULEENTRY32W
    prefix = 'Process32' if pid is None else 'Module32'
    first, next_entry = getattr(api, prefix+'FirstW'), getattr(api, prefix+'NextW')
    for fn in (first, next_entry):
        fn.argtypes = [W.HANDLE, C.POINTER(entry_type)]
        fn.restype = W.BOOL
    # ERROR_BAD_LENGTH can occur when the loader changes during a module snapshot.
    for attempt in range(3):
        handle = api.CreateToolhelp32Snapshot(flags, pid or 0)
        if handle != C.c_void_p(-1).value:
            break
        error = last_error()
        if error != 24 or pid is None or attempt == 2:
            raise api_failure('CreateToolhelp32Snapshot',error,stage=stage,flags=hex(flags),pid=pid)
        LOG.debug('Retry module snapshot after ERROR_BAD_LENGTH attempt=%s',attempt+1)
        time.sleep(.03)
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
        error = last_error()
        if error != 18:
            raise api_failure(prefix+'FirstW/NextW',error,stage=stage,pid=pid,entries=len(result))
    finally:
        api.CloseHandle(handle)
    LOG.info('Toolhelp succeeded stage=%s entries=%s',stage,len(result))
    return result


def parse_tasklist(text):
    result = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) >= 2 and row[0].casefold() == 'rustclient.exe' and row[1].isdigit():
            result.append((row[0],int(row[1])))
    return result


def find_rust_tasklist():
    executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'tasklist.exe'
    LOG.info('Trying tasklist fallback (RustClient.exe filter, no shell)')
    try:
        output = subprocess.run([str(executable), '/FI', 'IMAGENAME eq RustClient.exe', '/FO', 'CSV', '/NH'],
                                capture_output=True,encoding='utf-8',errors='replace',timeout=8,
                                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),check=True)
    except (OSError,subprocess.SubprocessError) as exc:
        LOG.error('tasklist failed: %s',exc)
        raise ReadFailure('tasklist process discovery failed: '+str(exc)) from exc
    result = parse_tasklist(output.stdout)
    LOG.info('tasklist Rust matches=%s PIDs=%s',len(result),[pid for _,pid in result])
    return result


def enumerate_modules_psapi(pid):
    """Documented alternate enumeration, requiring ordinary read permission."""
    api = C.WinDLL('kernel32',use_last_error=True)
    api.OpenProcess.argtypes = [W.DWORD,W.BOOL,W.DWORD]
    api.OpenProcess.restype = W.HANDLE
    api.CloseHandle.argtypes = [W.HANDLE]
    handle = api.OpenProcess(0x410,False,pid)  # QUERY_INFORMATION | VM_READ
    if not handle:
        raise api_failure('OpenProcess (PSAPI module enumeration)',last_error(),pid=pid,access='0x410')
    psapi = C.WinDLL('psapi',use_last_error=True)
    class MODULEINFO(C.Structure):
        _fields_ = [('base',C.c_void_p),('size',W.DWORD),('entry',C.c_void_p)]
    psapi.EnumProcessModulesEx.argtypes = [W.HANDLE,C.POINTER(W.HMODULE),W.DWORD,C.POINTER(W.DWORD),W.DWORD]
    psapi.EnumProcessModulesEx.restype = W.BOOL
    psapi.GetModuleBaseNameW.argtypes = [W.HANDLE,W.HMODULE,W.LPWSTR,W.DWORD]
    psapi.GetModuleBaseNameW.restype = W.DWORD
    psapi.GetModuleInformation.argtypes = [W.HANDLE,W.HMODULE,C.POINTER(MODULEINFO),W.DWORD]
    psapi.GetModuleInformation.restype = W.BOOL
    try:
        capacity = 256
        for _ in range(4):
            modules = (W.HMODULE*capacity)()
            needed = W.DWORD()
            if not psapi.EnumProcessModulesEx(handle,modules,C.sizeof(modules),C.byref(needed),3):
                raise api_failure('EnumProcessModulesEx',last_error(),pid=pid)
            if needed.value <= C.sizeof(modules):
                break
            capacity = needed.value//C.sizeof(W.HMODULE)+32
        else:
            raise ReadFailure('Module list kept growing during PSAPI enumeration')
        result = []
        for module in modules[:needed.value//C.sizeof(W.HMODULE)]:
            name = C.create_unicode_buffer(260)
            info = MODULEINFO()
            if not psapi.GetModuleBaseNameW(handle,module,name,len(name)):
                raise api_failure('GetModuleBaseNameW',last_error(),pid=pid)
            if not psapi.GetModuleInformation(handle,module,C.byref(info),C.sizeof(info)):
                raise api_failure('GetModuleInformation',last_error(),pid=pid)
            result.append((name.value,int(info.base or 0),int(info.size)))
        LOG.info('PSAPI enumeration succeeded pid=%s modules=%s',pid,len(result))
        return result
    finally:
        api.CloseHandle(handle)


def enumerate_windows(pid=None):
    if os.name != 'nt' or C.sizeof(C.c_void_p) != 8:
        raise RuntimeError('Live mode requires 64-bit Python on Windows.')
    api = C.WinDLL('kernel32', use_last_error=True)
    return enumerate_with_fallback(api,pid)


def enumerate_with_fallback(api,pid=None):
    try:
        return enumerate_toolhelp(api,pid)
    except ReadFailure as primary:
        LOG.warning('Toolhelp failed; trying documented alternate enumeration: %s',primary)
        try:
            return find_rust_tasklist() if pid is None else enumerate_modules_psapi(pid)
        except ReadFailure as secondary:
            raise ReadFailure(f'Primary: {primary}\nAlternate: {secondary}') from secondary


class Reader(QThread):
    snapshot_ready = Signal(list)
    status_changed = Signal(str)

    def __init__(self, pid, base, offset, width, height, trace=False):
        super().__init__()
        self.args = pid, base, offset, width, height
        self.trace = trace

    def run(self):
        memory = None
        last_status = 0
        try:
            pid, base, offset, width, height = self.args
            LOG.info('Reader start pid=%s offset=%#x dimensions=%sx%s candidates=%s',
                     pid,offset,width,height,base)
            memory = WindowsMemory(pid)
            memory.cancelled = self.isInterruptionRequested
            memory.trace_reads = self.trace
            if isinstance(base, list):
                layout = None
                failures = []
                for name, candidate in base:
                    if self.isInterruptionRequested():
                        return
                    trial = SuppliedLayout(memory, candidate, offset, width, height)
                    LOG.info('Testing module=%s base=%#x GOM_root=%#x',name,candidate,candidate+offset)
                    try:
                        trial.snapshot()
                        layout = trial
                        self.status_changed.emit(f'Supplied camera layout readable in {name}; '
                                                 'game-build compatibility is still unverified.')
                        break
                    except ReadFailure as exc:
                        LOG.warning('Module test failed name=%s reason=%s',name,exc)
                        failures.append(f'{name}: {exc}')
                if layout is None:
                    raise ReadFailure('Supplied layout failed in every candidate module. '
                                      'See the debug log for each API/read stage. '+ '; '.join(failures[:3]))
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
                    LOG.debug('%s counters=%s',status,layout.stats)
                    last_status = started
                self.msleep(50)
        except (ReadFailure, RuntimeError, OSError) as exc:
            LOG.exception('Reader stopped after error')
            self.snapshot_ready.emit([])
            self.status_changed.emit(str(exc))
        except Exception as exc:
            LOG.exception('Unexpected reader exception')
            self.snapshot_ready.emit([])
            self.status_changed.emit('Unexpected reader error: '+str(exc)+'; see debug log.')
        finally:
            if memory:
                memory.close()
            LOG.info('Reader thread finished')


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
        self.setWindowTitle('Rust overlay — '+VERSION)
        self.resize(680, 880)
        self.settings = QSettings('ExternalOverlayPrototype', 'RustOverlay')
        self.overlay = Overlay()
        self.reader = None
        outer = QVBoxLayout(self)
        warning = QLabel('Using your supplied offsets unchanged for build 26140533. '
                         'The debug panel records the exact failing Windows API or memory-read stage.')
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
        self.manual_pid = QLineEdit()
        self.manual_pid.setPlaceholderText('RustClient.exe PID from Task Manager → Details')
        use_pid = QPushButton('Use PID')
        use_pid.clicked.connect(self.use_manual_pid)
        manual_row = QHBoxLayout()
        manual_row.addWidget(self.manual_pid); manual_row.addWidget(use_pid)
        form.addRow('Manual fallback', manual_row)
        form.addRow('Module owning GOM offset', self.modules)
        note = QLabel('Auto checks your supplied layout against loaded modules using read-only access. '
                      'Process discovery, module discovery, and memory reads have separate diagnostics.')
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
        self.trace_reads = QCheckBox('Trace every memory read (very verbose; restart reader to apply)')
        outer.addWidget(self.trace_reads)
        self.debug_panel = QPlainTextEdit()
        self.debug_panel.setReadOnly(True)
        self.debug_panel.setMaximumBlockCount(1000)
        self.debug_panel.setFixedHeight(150)
        outer.addWidget(self.debug_panel)
        self.log_bridge = LogBridge(self)
        self.log_bridge.line.connect(self.debug_panel.appendPlainText)
        self.log_handler = MenuLogHandler(self.log_bridge)
        LOG.addHandler(self.log_handler)
        copy_debug = QPushButton('Copy debug log')
        copy_debug.clicked.connect(self.copy_debug)
        outer.addWidget(copy_debug)
        log_location = QLabel('Log file: '+str(LOG_PATH or 'not enabled (launch using main)'))
        log_location.setWordWrap(True)
        log_location.setTextInteractionFlags(Qt.TextSelectableByMouse)
        outer.addWidget(log_location)
        exit_button = QPushButton('Exit')
        exit_button.clicked.connect(self.close); outer.addWidget(exit_button)
        self.apply_options()
        LOG.info('Menu ready version=%s display=%sx%s device_pixel_ratio=%s',VERSION,
                 self.overlay.width(),self.overlay.height(),self.overlay.devicePixelRatioF())
        self.refresh_processes()

    def copy_debug(self):
        for handler in LOG.handlers:
            handler.flush()
        try:
            text = LOG_PATH.read_text(encoding='utf-8')[-500_000:] if LOG_PATH else self.debug_panel.toPlainText()
        except OSError:
            text = self.debug_panel.toPlainText()
        QApplication.clipboard().setText(f'Rust overlay {VERSION}\n'+text)
        self.status.setText('Debug log copied. Review it before sharing; it contains module addresses and API errors.')

    def use_manual_pid(self):
        if self.reader is not None:
            self.status.setText('Stop the reader before changing PID.')
            return
        try:
            pid = int(self.manual_pid.text().strip())
            if not 0 < pid <= 0xFFFFFFFF:
                raise ValueError()
        except ValueError:
            self.status.setText('Enter a positive numeric RustClient.exe PID from Task Manager → Details.')
            return
        LOG.info('Manual Rust PID selected=%s',pid)
        self.process.blockSignals(True)
        self.process.clear()
        self.process.addItem(f'Manual Rust PID {pid}',pid)
        self.process.blockSignals(False)
        self.refresh_modules()

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
        if self.reader is not None:
            self.status.setText('Stop the reader before refreshing processes.')
            return
        LOG.info('USER ACTION Find Rust process')
        self.process.blockSignals(True)
        self.process.clear()
        try:
            for name,pid in enumerate_windows():
                if name.casefold() == 'rustclient.exe': self.process.addItem(f'{name} (PID {pid})',pid)
            if self.process.count() == 0: self.status.setText('RustClient.exe not found. Start Rust or use demo.')
            else: self.status.setText('Rust process found; discovering modules…')
        except (RuntimeError,ReadFailure,OSError) as exc:
            LOG.exception('Process discovery failed before module enumeration')
            self.status.setText(str(exc)+' Demo remains available.')
        self.process.blockSignals(False)
        self.refresh_modules()

    def refresh_modules(self):
        self.modules.clear()
        pid = self.process.currentData()
        if pid is None: return
        LOG.info('Discovering modules pid=%s',pid)
        try:
            modules = enumerate_windows(pid)
            self.modules.addItem('Auto — check supplied layout', modules)
            for name,base,size in modules:
                LOG.debug('Loaded module name=%s base=%#x size=%#x',name,base,size)
                self.modules.addItem(f'{name} — {base:#x}', (base,size))
            self.status.setText(f'Process found: PID {pid}; {len(modules)} modules discovered. Ready to attempt read-only access.')
        except (RuntimeError,ReadFailure,OSError) as exc:
            LOG.exception('Module discovery failed pid=%s (process already found)',pid)
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
            self.reader = Reader(pid,base,offset,self.overlay.width(),self.overlay.height(),self.trace_reads.isChecked())
            worker = self.reader
            self.reader.snapshot_ready.connect(
                lambda entities: self.overlay.accept_snapshot(entities) if self.reader is worker else None)
            self.reader.status_changed.connect(
                lambda text: self.status.setText(text) if self.reader is worker else None)
            self.process.setEnabled(False); self.modules.setEnabled(False); self.offset.setEnabled(False)
            self.overlay.show()
            self.status.setText('Opening read-only access with your supplied profile…')
            self.reader.start()
        except (ValueError,TypeError) as exc:
            self.status.setText(str(exc))

    def stop(self):
        LOG.info('USER ACTION Stop / clear')
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
        LOG.removeHandler(self.log_handler)
        self.log_handler.close()
        event.accept()


def main():
    try:
        configure_logging()
    except OSError as exc:
        # Keep the app usable if user-profile log storage is unavailable.
        print('Could not create persistent debug log:',exc,file=sys.stderr)
    def report_exception(exc_type, value, traceback):
        LOG.critical('Unhandled Python exception',exc_info=(exc_type,value,traceback))
        sys.__excepthook__(exc_type,value,traceback)
    sys.excepthook = report_exception
    app = QApplication(sys.argv)
    menu = Menu()
    menu.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
