"""Run: QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest -v test_single_overlay"""
import struct
import tempfile
import unittest
from unittest.mock import patch

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


if __name__ == '__main__':
    unittest.main()
