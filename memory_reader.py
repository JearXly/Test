"""Read-only Windows adapter for the user-supplied, UNVERIFIED Rust layout."""
import argparse
import ctypes as C
from ctypes import wintypes as W
import json
import math
import os
from pathlib import Path
import socket
import struct
import time


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


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid', type=int, required=True, help='RustClient.exe PID from Task Manager')
    p.add_argument('--module-base', type=lambda s:int(s,0), required=True,
                   help='Verified base address of the module owning the GOM offset; not an absolute GOM address')
    p.add_argument('--gom-offset', type=lambda s:int(s,0), default=0x125D5C0)
    p.add_argument('--width', type=int, default=1920)
    p.add_argument('--height', type=int, default=1080)
    p.add_argument('--port', type=int, default=27841)
    p.add_argument('--hz', type=int, default=20)
    args = p.parse_args()
    if args.width <= 0 or args.height <= 0 or not 1 <= args.hz <= 120:
        p.error('Positive dimensions and --hz between 1 and 120 required')
    print('UNVERIFIED layout: build 26140533 compatibility is NOT established.')
    print('Read-only access, approximate standing boxes; no team/bone/visibility data.')
    m = WindowsMemory(args.pid)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    layout = SuppliedLayout(m, args.module_base, args.gom_offset, args.width, args.height)
    last_report = 0
    try:
        while True:
            started = time.monotonic()
            try:
                entities = layout.snapshot()
                # UDP size bound: each snapshot is complete, never fragmented by us.
                data = json.dumps({'entities':entities[:128]}, ensure_ascii=True).encode()
                while len(data) > 60000:
                    entities = entities[:len(entities)//2]
                    data = json.dumps({'entities':entities}, ensure_ascii=True).encode()
                sock.sendto(data, ('127.0.0.1', args.port))
                if started-last_report >= 5:
                    print(f'Snapshot: {len(entities)} projected players')
                    last_report = started
            except ReadFailure as exc:
                # Clear old marks immediately on incompatible/global reads.
                sock.sendto(b'{"entities":[]}', ('127.0.0.1',args.port))
                if started-last_report >= 5:
                    print('READ FAILED:', exc)
                    last_report = started
            time.sleep(max(0, 1/args.hz-(time.monotonic()-started)))
    except KeyboardInterrupt:
        pass
    finally:
        m.close()
        sock.close()


if __name__ == '__main__':
    main()
