"""Unit tests for the pure-Python RFB client against a tiny fake VNC server."""

import socket
import struct
import threading
import time
import unittest

from etcode_manager.rfb import RFBClient, RFBError


class FakeVNCServer(threading.Thread):
    """Minimal RFB 3.8 server: one raw frame, records input events."""

    def __init__(self, width=4, height=3, frame_color=b"\x11\x22\x33\x44"):
        super().__init__(daemon=True)
        self.width, self.height = width, height
        self.frame_color = frame_color
        self.key_events = []
        self.pointer_events = []
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.error = None

    def run(self) -> None:
        try:
            conn, _ = self.listener.accept()
            conn.sendall(b"RFB 003.008\n")
            conn.recv(12)                       # client version
            conn.sendall(b"\x01\x01")           # 1 security type: None
            conn.recv(1)                        # choice
            conn.sendall(struct.pack(">I", 0))  # security result OK
            conn.recv(1)                        # client init
            pixfmt = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
            conn.sendall(struct.pack(">HH", self.width, self.height) + pixfmt +
                         struct.pack(">I", 5) + b"fake-")
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                self._consume(conn, data)
        except Exception as exc:  # pragma: no cover
            self.error = exc

    def _consume(self, conn, data: bytes) -> None:
        # Enough for the fixed-size messages our client sends.
        while data:
            mtype = data[0]
            if mtype == 0 and len(data) >= 20:      # SetPixelFormat
                data = data[20:]
            elif mtype == 2 and len(data) >= 4:     # SetEncodings
                (count,) = struct.unpack(">H", data[2:4])
                need = 4 + 4 * count
                if len(data) < need:
                    break
                data = data[need:]
            elif mtype == 3 and len(data) >= 10:    # FramebufferUpdateRequest
                incremental = data[1]
                if not incremental:
                    self._send_frame(conn)
                data = data[10:]
            elif mtype == 4 and len(data) >= 8:     # KeyEvent
                self.key_events.append((data[1] == 1, struct.unpack(">I", data[4:8])[0]))
                data = data[8:]
            elif mtype == 5 and len(data) >= 6:     # PointerEvent
                self.pointer_events.append((data[1], *struct.unpack(">HH", data[2:6])))
                data = data[6:]
            else:
                break

    def _send_frame(self, conn) -> None:
        rect = struct.pack(">HHHHi", 0, 0, self.width, self.height, 0)  # raw
        payload = self.frame_color * (self.width * self.height)
        conn.sendall(b"\x00\x00" + struct.pack(">H", 1) + rect + payload)


class RFBClientTest(unittest.TestCase):
    def _connect(self, server):
        frames, state = [], {}

        def on_frame(rects, w, h, fb):
            frames.append((rects, w, h, fb))

        def on_disconnect(reason):
            state["reason"] = reason

        client = RFBClient("127.0.0.1", server.port,
                           on_frame=on_frame, on_disconnect=on_disconnect)
        client.connect(timeout=5)
        return client, frames, state

    def test_handshake_and_frame(self):
        server = FakeVNCServer()
        server.start()
        client, frames, state = self._connect(server)
        deadline = time.time() + 5
        while not frames and time.time() < deadline:
            time.sleep(0.01)
        client.close()

        self.assertTrue(frames, "no framebuffer update received")
        rects, w, h, fb = frames[0]
        self.assertEqual((w, h), (4, 3))
        self.assertEqual(rects, [(0, 0, 4, 3)])
        self.assertEqual(fb[:4], b"\x11\x22\x33\x44")
        self.assertEqual(len(fb), 4 * 3 * 4)

    def test_input_events_reach_server(self):
        server = FakeVNCServer()
        server.start()
        client, frames, _ = self._connect(server)
        deadline = time.time() + 5
        while not frames and time.time() < deadline:
            time.sleep(0.01)

        client.send_key(0x61, True)     # 'a' down
        client.send_key(0x61, False)    # 'a' up
        client.send_pointer(0x01, 2, 1)  # left button at (2,1)
        deadline = time.time() + 5
        while (len(server.key_events) < 2 or len(server.pointer_events) < 1) \
                and time.time() < deadline:
            time.sleep(0.01)
        client.close()

        self.assertEqual(server.key_events, [(True, 0x61), (False, 0x61)])
        self.assertEqual(server.pointer_events, [(0x01, 2, 1)])

    def test_connection_refused(self):
        client = RFBClient("127.0.0.1", 1)  # nothing listens there
        with self.assertRaises(RFBError):
            client.connect(timeout=1)


if __name__ == "__main__":
    unittest.main()