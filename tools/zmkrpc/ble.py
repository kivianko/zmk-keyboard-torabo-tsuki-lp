"""ZMK Studio RPC の BLEトランスポート (macOS CoreBluetooth)

接続済みのキーボード(HIDとしてMacに繋がっている状態)ともRPCで会話できる。
フレーミング(SOF/ESC/EOF)はUSBシリアルと同一で、client.pyの実装を共用する。
"""
import os, sys, time

sys.path.insert(0, os.path.dirname(__file__))
import studio_pb2  # noqa: E402
from client import SOF, ESC, EOF  # noqa: E402

from CoreBluetooth import (  # noqa: E402
    CBCentralManager, CBUUID, CBCharacteristicWriteWithResponse,
)
from Foundation import NSObject, NSRunLoop, NSDate  # noqa: E402

SVC_UUID = "00000000-0196-6107-c967-c5cfb1c2482a"
RPC_UUID = "00000001-0196-6107-c967-c5cfb1c2482a"


def _pump(cond, timeout, what):
    deadline = time.time() + timeout
    while not cond():
        if time.time() > deadline:
            raise TimeoutError(f"BLE: {what} がタイムアウトしました")
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))


class _Delegate(NSObject):
    def init(self):
        self = objc_super_init(self)
        self.state = None
        self.connected = False
        self.services_done = False
        self.chars_done = False
        self.wrote = False
        self.notif_ready = False
        self.rx = bytearray()
        self.frames = []
        self._in_frame = False
        self._esc = False
        return self

    # --- central ---
    def centralManagerDidUpdateState_(self, c):
        self.state = c.state()

    def centralManager_didConnectPeripheral_(self, c, p):
        self.connected = True

    def centralManager_didFailToConnectPeripheral_error_(self, c, p, e):
        self.connected = False

    # --- peripheral ---
    def peripheral_didDiscoverServices_(self, p, error):
        self.services_done = True

    def peripheral_didDiscoverCharacteristicsForService_error_(self, p, svc, error):
        self.chars_done = True

    def peripheral_didWriteValueForCharacteristic_error_(self, p, ch, error):
        self.wrote = True

    def peripheral_didUpdateNotificationStateForCharacteristic_error_(self, p, ch, error):
        if error is None:
            self.notif_ready = True

    def peripheral_didUpdateValueForCharacteristic_error_(self, p, ch, error):
        data = ch.value()
        if data is None:
            return
        for b in bytes(data):
            if not self._in_frame:
                if b == SOF:
                    self._in_frame = True
                    self.rx.clear()
                continue
            if self._esc:
                self.rx.append(b)
                self._esc = False
            elif b == ESC:
                self._esc = True
            elif b == EOF:
                self.frames.append(bytes(self.rx))
                self._in_frame = False
            elif b == SOF:
                self.rx.clear()
            else:
                self.rx.append(b)


def objc_super_init(obj):
    import objc
    return objc.super(_Delegate, obj).init()


class BLEStudioClient:
    def __init__(self):
        self.d = _Delegate.alloc().init()
        self.c = CBCentralManager.alloc().initWithDelegate_queue_(self.d, None)
        _pump(lambda: self.d.state is not None, 8, "Bluetooth初期化")
        if self.d.state != 5:
            raise RuntimeError(f"Bluetoothが使えません (state={self.d.state})")
        svc = CBUUID.UUIDWithString_(SVC_UUID)
        ps = self.c.retrieveConnectedPeripheralsWithServices_([svc])
        if not ps:
            raise RuntimeError("BLE接続中のキーボードが見つかりません (Macとペアリング/接続されていますか?)")
        self.p = ps[0]
        self.port = f"BLE:{self.p.name()}"
        self.p.setDelegate_(self.d)
        self.c.connectPeripheral_options_(self.p, None)
        _pump(lambda: self.d.connected, 8, "接続")
        self.p.discoverServices_([svc])
        _pump(lambda: self.d.services_done, 8, "サービス探索")
        target = next(s for s in self.p.services() if str(s.UUID().UUIDString()).lower() == SVC_UUID)
        self.p.discoverCharacteristics_forService_([CBUUID.UUIDWithString_(RPC_UUID)], target)
        _pump(lambda: self.d.chars_done, 8, "characteristic探索")
        self.ch = next(ch for ch in target.characteristics() if str(ch.UUID().UUIDString()).lower() == RPC_UUID)
        self.p.setNotifyValue_forCharacteristic_(True, self.ch)
        _pump(lambda: self.d.notif_ready, 8, "インジケーション購読")
        self._req_id = 0

    def close(self):
        try:
            self.p.setNotifyValue_forCharacteristic_(False, self.ch)
            self.c.cancelPeripheralConnection_(self.p)  # GATT購読解除(HID接続はOSが維持する)
        except Exception:
            pass

    @staticmethod
    def _frame(payload: bytes) -> bytes:
        out = bytearray([SOF])
        for b in payload:
            if b in (SOF, ESC, EOF):
                out.append(ESC)
            out.append(b)
        out.append(EOF)
        return bytes(out)

    def _write(self, data: bytes):
        from Foundation import NSData
        chunk = int(self.p.maximumWriteValueLengthForType_(CBCharacteristicWriteWithResponse)) or 20
        for i in range(0, len(data), chunk):
            self.d.wrote = False
            nsd = NSData.dataWithBytes_length_(data[i:i + chunk], len(data[i:i + chunk]))
            self.p.writeValue_forCharacteristic_type_(nsd, self.ch, CBCharacteristicWriteWithResponse)
            _pump(lambda: self.d.wrote, 5, "書き込み")

    def call(self, req, timeout=5.0):
        self._req_id += 1
        req.request_id = self._req_id
        self._write(self._frame(req.SerializeToString()))
        deadline = time.time() + timeout
        while time.time() < deadline:
            while self.d.frames:
                resp = studio_pb2.Response()
                resp.ParseFromString(self.d.frames.pop(0))
                if resp.WhichOneof("type") == "request_response":
                    rr = resp.request_response
                    if rr.request_id == self._req_id:
                        return rr
            NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))
        raise TimeoutError("BLE: RPC応答タイムアウト")


if __name__ == "__main__":
    c = BLEStudioClient()
    print(f"port: {c.port}")
    req = studio_pb2.Request()
    req.core.get_device_info = True
    rr = c.call(req)
    info = rr.core.get_device_info
    print(f"device: {info.name}  serial: {info.serial_number.hex()}")
    c.close()
