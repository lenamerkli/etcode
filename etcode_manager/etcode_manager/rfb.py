"""Pure-Python RFB (VNC) protocol client.

Connects to QEMU's built-in VNC server, decodes framebuffer updates and
sends keyboard / pointer events.  No external dependencies.

Supported: RFB 3.3 / 3.7 / 3.8, security type "None", raw encoding and the
DesktopSize pseudo-encoding, 32 bpp true-colour (BGRX / QImage layout).
This is deliberately minimal — it is exactly what QEMU's VNC server offers
on a loopback socket, and it gives full screen + input control without
changing the guest image (no xrdp/freeRDP/VNC server inside the VM).
"""

from __future__ import annotations

import socket
import struct
import threading

ENCODING_RAW = 0
ENCODING_DESKTOP_SIZE = -223

PIXEL_FORMAT = struct.pack(
    ">BBBBHHHBBB3x",
    32,   # bits per pixel
    24,   # depth
    0,    # big-endian flag (0 = little-endian pixel values)
    1,    # true-colour flag
    255,  # red max
    255,  # green max
    255,  # blue max
    16,   # red shift
    8,    # green shift
    0,    # blue shift
)


class RFBError(RuntimeError):
    """Protocol or connection error."""


class RFBClient:
    """Threaded RFB client.

    Callbacks (invoked from the reader thread — marshal to your GUI thread
    if needed):

    * ``on_connected(width, height, name)``
    * ``on_frame(rects, width, height, framebuffer)`` after each update batch;
      ``rects`` is a list of ``(x, y, w, h)`` and ``framebuffer`` the full
      screen contents as ``width * height * 4`` bytes (BGRX).
    * ``on_resize(width, height)``
    * ``on_disconnect(reason)``
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 5900,
        on_connected=None,
        on_frame=None,
        on_resize=None,
        on_disconnect=None,
    ):
        self._host = host
        self._port = port
        self._on_connected = on_connected
        self._on_frame = on_frame
        self._on_resize = on_resize
        self._on_disconnect = on_disconnect

        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._closing = False

        self.width = 0
        self.height = 0
        self.name = ""
        self._fb = bytearray()

    # -- lifecycle ----------------------------------------------------------

    def connect(self, timeout: float = 10.0) -> None:
        """Open the TCP connection and start the reader thread."""
        try:
            self._sock = socket.create_connection((self._host, self._port), timeout=timeout)
        except OSError as exc:
            raise RFBError(f"cannot connect to VNC on {self._host}:{self._port}: {exc}") from exc
        self._sock.settimeout(None)
        self._thread = threading.Thread(target=self._run, name="rfb-reader", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._closing = True
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    @property
    def connected(self) -> bool:
        return self._sock is not None

    # -- input --------------------------------------------------------------

    def send_key(self, keysym: int, down: bool) -> None:
        """Send a key event (X11 keysym, see keysyms.py)."""
        self._send(struct.pack(">BBHI", 4, 1 if down else 0, 0, keysym))

    def send_pointer(self, button_mask: int, x: int, y: int) -> None:
        """Send a pointer event.

        ``button_mask``: bit0=left, bit1=middle, bit2=right,
        bit3=wheel up, bit4=wheel down (clicks).
        """
        x = max(0, min(int(x), self.width - 1))
        y = max(0, min(int(y), self.height - 1))
        self._send(struct.pack(">BBHH", 5, button_mask, x, y))

    def _send(self, payload: bytes) -> None:
        sock = self._sock
        if sock is None:
            return
        with self._send_lock:
            try:
                sock.sendall(payload)
            except OSError:
                pass  # reader thread reports the disconnect

    # -- protocol -----------------------------------------------------------

    def _run(self) -> None:
        reason = "connection closed"
        try:
            self._handshake()
            if self._on_connected:
                self._on_connected(self.width, self.height, self.name)
            self._request_update(incremental=False)
            self._message_loop()
            reason = "server closed the connection"
        except (OSError, RFBError, struct.error) as exc:
            reason = str(exc) or exc.__class__.__name__
        finally:
            was_open = self._sock is not None
            self.close()
            if self._on_disconnect and (was_open or not self._closing):
                self._on_disconnect(reason if not self._closing else "disconnected")

    def _recv_exact(self, n: int) -> bytes:
        data = bytearray()
        while len(data) < n:
            chunk = self._sock.recv(n - len(data))
            if not chunk:
                raise RFBError("connection closed by VNC server")
            data += chunk
        return bytes(data)

    def _handshake(self) -> None:
        # Version
        version = self._recv_exact(12)
        if not version.startswith(b"RFB "):
            raise RFBError(f"not an RFB server: {version!r}")
        minor = int(version[7:11].replace(b".", b"").decode()[-2:])
        if minor < 3:
            raise RFBError(f"unsupported RFB version {version!r}")
        # Reply 3.8 to 3.8/3.7, else 3.3
        reply = b"RFB 003.008\n" if minor >= 7 else b"RFB 003.003\n"
        self._send(reply)

        if reply == b"RFB 003.008\n":
            n_types = self._recv_exact(1)[0]
            if n_types == 0:
                length = struct.unpack(">I", self._recv_exact(4))[0]
                raise RFBError(f"server refused connection: {self._recv_exact(length)!r}")
            types = self._recv_exact(n_types)
            if 1 not in types:  # 1 = "None"
                raise RFBError(f"unsupported security types {list(types)} (password auth not supported)")
            self._send(b"\x01")  # choose "None"
            if minor >= 8:
                result = struct.unpack(">I", self._recv_exact(4))[0]
                if result != 0:
                    raise RFBError("VNC security handshake failed")
        else:  # 3.3
            sec_type = struct.unpack(">I", self._recv_exact(4))[0]
            if sec_type == 0:
                length = struct.unpack(">I", self._recv_exact(4))[0]
                raise RFBError(f"server refused connection: {self._recv_exact(length)!r}")
            if sec_type != 1:
                raise RFBError(f"unsupported security type {sec_type} (password auth not supported)")

        # ClientInit / ServerInit
        self._send(b"\x01")  # shared flag
        width, height = struct.unpack(">HH", self._recv_exact(4))
        self._recv_exact(16)  # server pixel format (we override ours below)
        name_len = struct.unpack(">I", self._recv_exact(4))[0]
        self.name = self._recv_exact(name_len).decode("utf-8", "replace")
        self._set_size(width, height)

        # Our pixel format (BGRX, 32bpp) and encodings (raw + DesktopSize)
        self._send(b"\x00\x00\x00\x00" + PIXEL_FORMAT)
        self._send(struct.pack(">BBH", 2, 0, 2) + struct.pack(">ii", ENCODING_RAW, ENCODING_DESKTOP_SIZE))

    def _set_size(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        self._fb = bytearray(width * height * 4)

    def _request_update(self, incremental: bool) -> None:
        self._send(struct.pack(">BBHHHH", 3, 1 if incremental else 0, 0, 0, self.width, self.height))

    def _message_loop(self) -> None:
        while True:
            msg_type = self._recv_exact(1)[0]
            if msg_type == 0:  # FramebufferUpdate
                self._recv_exact(1)  # padding
                (n_rects,) = struct.unpack(">H", self._recv_exact(2))
                rects = []
                for _ in range(n_rects):
                    x, y, w, h, encoding = struct.unpack(">HHHHi", self._recv_exact(12))
                    if encoding == ENCODING_RAW:
                        data = self._recv_exact(w * h * 4)
                        fb_row_bytes = self.width * 4
                        src = 0
                        for row in range(y, y + h):
                            off = row * fb_row_bytes + x * 4
                            self._fb[off:off + w * 4] = data[src:src + w * 4]
                            src += w * 4
                        rects.append((x, y, w, h))
                    elif encoding == ENCODING_DESKTOP_SIZE:
                        self._set_size(w, h)
                        if self._on_resize:
                            self._on_resize(w, h)
                    else:
                        raise RFBError(f"unsupported encoding {encoding} (did the server honour SetEncodings?)")
                if self._on_frame and rects:
                    self._on_frame(rects, self.width, self.height, bytes(self._fb))
                self._request_update(incremental=True)
            elif msg_type == 1:  # SetColourMapEntries
                self._recv_exact(3)
                (first,) = struct.unpack(">H", self._recv_exact(2))
                (count,) = struct.unpack(">H", self._recv_exact(2))
                self._recv_exact(count * 6)
                del first
            elif msg_type == 2:  # Bell
                pass
            elif msg_type == 3:  # ServerCutText
                self._recv_exact(3)
                (length,) = struct.unpack(">I", self._recv_exact(4))
                self._recv_exact(length)
            else:
                raise RFBError(f"unknown server message type {msg_type}")