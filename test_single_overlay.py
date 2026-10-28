"""Run: QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest -v test_single_overlay"""
import struct
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from PySide6.QtCore import QSettings
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
import rust_overlay as target


class MemoryFixture:
    def __init__(self):
        self.data = {}
        self.closed = False
        self.cancelled = lambda: False

    def put(self, address, fmt, *values):
        for i, byte in enumerate(struct.pack('<'+fmt, *values)):
            self.data[address+i] = byte

    def unpack(self, address, fmt):
        if self.cancelled():
            raise target.ReadFailure('stopped')
        try:
            return struct.unpack('<'+fmt, bytes(self.data[address+i]
                                 for i in range(struct.calcsize('<'+fmt))))
        except KeyError:
            raise target.ReadFailure('invalid fixture address')

    def ptr(self, address):
        return self.unpack(address, 'Q')[0]

    def name(self, address):
        return 'Fixture player'

    def close(self):
        self.closed = True


def fixture():
    m = MemoryFixture()
    base, offset = 0x100000, 0x125D5C0
    m.put(base+offset, 'Q', 0x200000)
    m.put(0x200000, 'QQ', 0, 0x300000)
    m.put(0x300008, 'QQ', 0x300100, 0x400000)
    m.put(0x300108, 'QQ', 0, 0x400100)
    for obj,tag,comp,trans in [(0x400000,5,0x500000,0x600000),
                               (0x400100,6,0x500100,0x600100)]:
        m.put(obj+0x4C, 'H', tag)
        m.put(obj+0x28, 'Q', comp)
        m.put(comp+8, 'Q', trans)
    m.put(0x600040, 'fff', 0,0,0)
    m.put(0x600140, 'fff', 0,0,10)
    m.put(0x500018, 'Q', 0x700000)
    m.put(0x7000B8, '16f', 1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1)
    m.put(0x500118, 'Q', 0x800000)
    m.put(0x800020, 'Q', 0x900000)
    m.put(0x900148, 'B', 0)
    m.put(0x90014C, 'f', 75)
    m.put(0x900208, 'Q', 0xA00000)
    return m, base, offset


class SingleOverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings_dir = tempfile.TemporaryDirectory()
        QSettings.setDefaultFormat(QSettings.IniFormat)
        QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, cls.settings_dir.name)
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.settings_dir.cleanup()

    def setUp(self):
        QSettings('ExternalOverlayPrototype', 'RustOverlay').clear()

    def test_supplied_layout_projection_and_cycle(self):
        memory, base, offset = fixture()
        layout = target.SuppliedLayout(memory, base, offset, 1920,1080)
        player, = layout.snapshot()
        self.assertEqual(player['name'], 'Fixture player')
        self.assertEqual(player['distance'], 10)
        self.assertGreater(player['h'], 0)
        memory.put(0x300108, 'QQ', 0x300000, 0x400100)
        self.assertEqual(len(list(layout.objects())), 2)

    def test_missing_camera_reports_failure(self):
        memory, base, offset = fixture()
        memory.put(0x40004C, 'H', 0)
        with self.assertRaisesRegex(target.ReadFailure, 'camera'):
            target.SuppliedLayout(memory,base,offset,1920,1080).snapshot()

    def test_demo_controls_render_and_stop(self):
        with patch.object(target, 'enumerate_windows', return_value=[]):
            menu = target.Menu()
        menu.start_demo()
        menu.overlay.tick()
        self.app.processEvents()
        self.assertEqual(menu.overlay.entities[0]['name'], 'DEMO ENTITY')
        self.assertFalse(menu.overlay.grab().isNull())
        menu.spins['max_distance'].setValue(99)
        menu.checks['names'].setChecked(False)
        self.assertEqual(menu.overlay.config['max_distance'], 99)
        self.assertFalse(menu.overlay.config['names'])
        menu.stop()
        self.assertFalse(menu.overlay.isVisible())
        self.assertEqual(menu.overlay.entities, [])
        menu.close()

    def test_reader_delivers_snapshot_and_closes_handle_on_stop(self):
        memory, base, offset = fixture()
        def enumerate_fake(pid=None):
            return [('RustClient.exe',123)] if pid is None else [('fixture.dll',base,offset+8)]
        with patch.object(target,'enumerate_windows',side_effect=enumerate_fake), \
             patch.object(target,'WindowsMemory',return_value=memory):
            menu = target.Menu()
            self.assertEqual(menu.modules.currentIndex(), 0)
            menu.start_reader()
            for _ in range(30):
                QTest.qWait(20)
                if menu.overlay.entities:
                    break
            self.assertEqual(menu.overlay.entities[0]['name'], 'Fixture player')
            menu.stop()
            self.app.processEvents()
            self.assertTrue(memory.closed)
            self.assertEqual(menu.overlay.entities, [])
            menu.close()

    def test_auto_rejects_incompatible_layout(self):
        def enumerate_fake(pid=None):
            return [('RustClient.exe',123)] if pid is None else [('fixture.dll',0x100000,0x2000000)]
        with patch.object(target,'enumerate_windows',side_effect=enumerate_fake), \
             patch.object(target,'WindowsMemory',return_value=MemoryFixture()):
            menu = target.Menu()
            menu.start_reader()
            for _ in range(30):
                QTest.qWait(20)
                if 'failed in every candidate' in menu.status.text():
                    break
            self.assertIn('failed in every candidate',menu.status.text())
            self.assertEqual(menu.overlay.entities, [])
            menu.close()

    def test_access_failure_reaches_menu(self):
        def enumerate_fake(pid=None):
            return [('RustClient.exe',123)] if pid is None else [('fixture.dll',0x100000,0x2000000)]
        with patch.object(target,'enumerate_windows',side_effect=enumerate_fake), \
             patch.object(target,'WindowsMemory',side_effect=target.ReadFailure('Access denied fixture')):
            menu = target.Menu()
            menu.modules.setCurrentIndex(0)
            menu.start_reader()
            for _ in range(30):
                QTest.qWait(20)
                if 'Access denied fixture' in menu.status.text():
                    break
            self.assertIn('Access denied fixture',menu.status.text())
            self.assertEqual(menu.overlay.entities, [])
            menu.close()

    def test_snapshot_denied_identifies_api_flags_and_pid(self):
        api = MagicMock()
        api.CreateToolhelp32Snapshot.return_value = target.C.c_void_p(-1).value
        with patch.object(target,'last_error',return_value=5):
            with self.assertRaisesRegex(target.ReadFailure,'CreateToolhelp32Snapshot.*error 5.*module list for PID 123'):
                target.enumerate_toolhelp(api,123)
        api.CreateToolhelp32Snapshot.assert_called_once_with(0x18,123)
        api.CloseHandle.assert_not_called()

    def test_transient_module_snapshot_retries_only_bad_length(self):
        api = MagicMock()
        api.CreateToolhelp32Snapshot.side_effect = [target.C.c_void_p(-1).value,1234]
        api.Module32FirstW.return_value = 0
        with patch.object(target,'last_error',side_effect=[24,18]), patch.object(target.time,'sleep'):
            self.assertEqual(target.enumerate_toolhelp(api,123),[])
        self.assertEqual(api.CreateToolhelp32Snapshot.call_count,2)
        api.CloseHandle.assert_called_once_with(1234)

    def test_process_fallback_does_not_request_module_access(self):
        with patch.object(target,'enumerate_toolhelp',side_effect=target.ReadFailure('snapshot denied')), \
             patch.object(target,'find_rust_tasklist',return_value=[('RustClient.exe',123)]) as fallback, \
             patch.object(target,'enumerate_modules_psapi') as modules:
            self.assertEqual(target.enumerate_with_fallback(object()),[('RustClient.exe',123)])
            fallback.assert_called_once()
            modules.assert_not_called()

    def test_module_fallback_preserves_both_access_errors(self):
        with patch.object(target,'enumerate_toolhelp',side_effect=target.ReadFailure('snapshot error 5')), \
             patch.object(target,'enumerate_modules_psapi',side_effect=target.ReadFailure('OpenProcess error 5')):
            with self.assertRaises(target.ReadFailure) as caught:
                target.enumerate_with_fallback(object(),123)
            self.assertIn('Primary: snapshot error 5',str(caught.exception))
            self.assertIn('Alternate: OpenProcess error 5',str(caught.exception))

    def test_tasklist_parses_filtered_csv_without_localized_headers(self):
        text = '"RustClient.exe","123","Console","1","1,234 K"\nINFO: no tasks\n"Other.exe","456"\n'
        self.assertEqual(target.parse_tasklist(text),[('RustClient.exe',123)])

    def test_manual_pid_retains_process_when_module_access_denied(self):
        with patch.object(target,'enumerate_windows',return_value=[]):
            menu = target.Menu()
        menu.manual_pid.setText('123')
        with patch.object(target,'enumerate_windows',side_effect=target.ReadFailure('module API error 5')):
            menu.use_manual_pid()
        self.assertEqual(menu.process.currentData(),123)
        self.assertIn('module API error 5',menu.status.text())
        self.assertEqual(menu.modules.count(),0)
        menu.close()

    def test_persistent_debug_log_and_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            old_path = target.LOG_PATH
            old_handlers = list(target.LOG.handlers)
            try:
                path = target.configure_logging(directory)
                with patch.object(target,'enumerate_windows',return_value=[]):
                    menu = target.Menu()
                target.LOG.error('API diagnostic fixture, error 5')
                menu.copy_debug()
                self.assertIn('API diagnostic fixture',QApplication.clipboard().text())
                self.assertIn(target.VERSION,path.read_text())
                menu.close()
            finally:
                for handler in list(target.LOG.handlers):
                    if handler not in old_handlers:
                        target.LOG.removeHandler(handler)
                        handler.close()
                target.LOG_PATH = old_path


if __name__ == '__main__':
    unittest.main()
