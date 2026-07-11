"""ZMK Studio RPC クライアント (USBシリアル直結、pyserial不要)

フレーミング: SOF=0xAB, ESC=0xAC, EOF=0xAD (ペイロード中の特殊バイトはESC前置)
ペイロード: protobuf zmk.studio.Request / Response
"""
import os, sys, glob, select, termios, time

sys.path.insert(0, os.path.dirname(__file__))
import studio_pb2  # noqa: E402

SOF, ESC, EOF = 0xAB, 0xAC, 0xAD


def find_port():
    ports = sorted(glob.glob("/dev/cu.usbmodem*"))
    if not ports:
        raise RuntimeError("キーボードのシリアルポートが見つかりません")
    if len(ports) > 1:
        raise RuntimeError(f"ポートが複数あります。PORT=で指定してください: {ports}")
    return ports[0]


class StudioClient:
    def __init__(self, port=None):
        self.port = port or os.environ.get("PORT") or find_port()
        self.fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        a = termios.tcgetattr(self.fd)
        # raw mode / 115200bps (1200bpsはブートローダtrigger予約なので厳禁)
        a[0] = a[1] = a[3] = 0
        a[2] = termios.CREAD | termios.CLOCAL | termios.CS8
        a[4] = a[5] = termios.B115200
        termios.tcsetattr(self.fd, termios.TCSANOW, a)
        termios.tcflush(self.fd, termios.TCIOFLUSH)
        self._req_id = 0

    def close(self):
        os.close(self.fd)

    @staticmethod
    def _frame(payload: bytes) -> bytes:
        out = bytearray([SOF])
        for b in payload:
            if b in (SOF, ESC, EOF):
                out.append(ESC)
            out.append(b)
        out.append(EOF)
        return bytes(out)

    def _read_frame(self, timeout=3.0) -> bytes:
        buf = bytearray()
        in_frame = False
        esc = False
        deadline = time.time() + timeout
        while time.time() < deadline:
            r, _, _ = select.select([self.fd], [], [], 0.1)
            if not r:
                continue
            for b in os.read(self.fd, 4096):
                if not in_frame:
                    if b == SOF:
                        in_frame = True
                        buf.clear()
                    continue
                if esc:
                    buf.append(b)
                    esc = False
                elif b == ESC:
                    esc = True
                elif b == EOF:
                    return bytes(buf)
                elif b == SOF:
                    buf.clear()  # 不正: フレーム先頭からやり直し
                else:
                    buf.append(b)
        raise TimeoutError("RPC応答タイムアウト")

    def call(self, req: "studio_pb2.Request", timeout=3.0):
        self._req_id += 1
        req.request_id = self._req_id
        os.write(self.fd, self._frame(req.SerializeToString()))
        # notificationは読み飛ばし、自分のrequest_idの応答を待つ
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = studio_pb2.Response()
            resp.ParseFromString(self._read_frame(timeout=deadline - time.time()))
            if resp.WhichOneof("type") == "request_response":
                rr = resp.request_response
                if rr.request_id == self._req_id:
                    return rr
        raise TimeoutError("対応するRPC応答が来ません")


def open_client(prefer=None):
    """トランスポート自動選択: USBポートがあればUSB、無ければBLE。ZMK_TRANSPORT=usb|ble で強制"""
    prefer = prefer or os.environ.get("ZMK_TRANSPORT")
    if prefer == "ble":
        from ble import BLEStudioClient
        return BLEStudioClient()
    if prefer == "usb":
        return StudioClient()
    try:
        return StudioClient()
    except RuntimeError as e:
        if "見つかりません" not in str(e):
            raise
        from ble import BLEStudioClient
        return BLEStudioClient()


if __name__ == "__main__":
    c = StudioClient()
    print(f"port: {c.port}")
    req = studio_pb2.Request()
    req.core.get_device_info = True
    rr = c.call(req)
    info = rr.core.get_device_info
    print(f"device: {info.name}  serial: {info.serial_number.hex()}")
    c.close()
