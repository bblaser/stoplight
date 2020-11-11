'''
This class is used to communicate between stoplights with socket connections.
'''
import sys
import selectors
import json
import io
import struct


class Server:
    """
    See: :ref:`Server`
    """
    # pylint: disable=too-many-instance-attributes
    def __init__(self, selector, sock, addr, verbose=False):
        self.selector = selector
        self.sock = sock
        self.addr = addr
        self.verbose = verbose
        self._recv_buffer = b""
        self._send_buffer = b""
        self._jsonheader_len = None
        self._jsonheader = None
        self.jsonheader = None
        self.request = None
        self.response_created = False

    def _set_selector_events_mask(self, mode):
        """Set selector to listen for events: mode is 'r', 'w', or 'rw'."""
        if mode == "r":
            events = selectors.EVENT_READ
        elif mode == "w":
            events = selectors.EVENT_WRITE
        elif mode == "rw":
            events = selectors.EVENT_READ | selectors.EVENT_WRITE
        else:
            raise ValueError(f"Invalid events mask mode {repr(mode)}.")
        self.selector.modify(self.sock, events, data=self)

    def _read(self):
        try:
            # should be ready to read
            data = self.sock.recv(4096)
        except BlockingIOError:
            # resource temporarialy unavailable (errno EWOULDBLOCK)
            pass
        else:
            if data:
                self._recv_buffer += data
            else:
                raise RuntimeError("Peer closed.")

    def _write(self):
        if self._send_buffer:
            if self.verbose:
                print("sending", repr(self._send_buffer), "to", self.addr)
            try:
                # should be ready to write
                sent = self.sock.send(self._send_buffer)
            except BlockingIOError:
                # resource temporarialy unavailable (errno EWOULDBLOCK)
                pass
            else:
                self._send_buffer = self._send_buffer[sent:]
                # close when the buffer is drained. The response has been sent
                if sent and not self._send_buffer:
                    self.close()

    @classmethod
    def _json_encode(cls, obj, encoding):
        return json.dumps(obj, ensure_ascii=False).encode(encoding)

    @classmethod
    def _json_decode(cls, json_bytes, encoding):
        tiow = io.TextIOWrapper(
            io.BytesIO(json_bytes), encoding=encoding, newline=""
        )
        obj = json.load(tiow)
        tiow.close()
        return obj

    def _create_message(
            self, *, content_bytes, content_type, content_encoding
    ):
        jsonheader = {
            "byteorder": sys.byteorder,
            "content-type": content_type,
            "content-encoding": content_encoding,
            "content-length": len(content_bytes),
        }
        jsonheader_bytes = self._json_encode(jsonheader, "utf-8")
        message_hdr = struct.pack(">H", len(jsonheader_bytes))
        message = message_hdr + jsonheader_bytes + content_bytes
        return message

    def _create_response_json_content(self):
        # Check on the LED status and sendback "their" status as displayed here
        # query = what_they_sent
        # answer = what_we_have_them_set_to
        answer = 1  # for testing
        content = {"result": answer}

        content_encoding = "utf-8"
        response = {
            "content_bytes": self._json_encode(content, content_encoding),
            "content_type": "text/json",
            "content_encoding": content_encoding,
        }
        return response

    @classmethod
    def _create_empty_response_content(cls):
        response = {
            "content_bytes": None,
            "content_type": None,
            "content_encoding": "binary",
        }
        return response

    def process_events(self, mask):
        """
        Either call self.read() or self.write() based on the selector
        """
        if mask & selectors.EVENT_READ:
            self.read()
        if mask & selectors.EVENT_WRITE:
            self.write()

    def read(self):
        '''
        Read data from the socket and store it in the receive buffer, process the data,
        then process the request.
        '''
        self._read()

        if self._jsonheader_len is None:
            self.process_protoheader()

        if self._jsonheader_len is not None:
            if self.jsonheader is None:
                self.process_jsonheader()

        if self.jsonheader:
            if self.request is None:
                self.process_request()

    def write(self):
        '''
        Create a response and send it.
        '''
        if self.request:
            if not self.response_created:
                self.create_response()

        self._write()

    def close(self):
        '''
        Unregister from the selector and close socket connection
        '''
        if self.verbose:
            print("closing connection to", self.addr)
        try:
            self.selector.unregister(self.sock)
        except Exception as exc:  # pylint: disable=broad-except
            print(
                "error: selector.unregister() exception for",
                f"{self.addr}: {repr(exc)}",
            )

        try:
            self.sock.close()
        except OSError as exc:
            print(
                "error:sockett.close() exception for",
                f"{self.addr}: {repr(exc)}",
            )
        finally:
            # Delete reference to socket object for garbage collection
            self.sock = None

    def process_protoheader(self):
        '''
        Fixed-length header; output self._jsonheader_len
        '''
        hdrlen = 2
        if len(self._recv_buffer) >= hdrlen:
            self._jsonheader_len = struct.unpack(
                ">H", self._recv_buffer[:hdrlen]
            )[0]
            self._recv_buffer = self._recv_buffer[hdrlen:]

    def process_jsonheader(self):
        '''
        JSON header; output self.jsonheader
        '''
        hdrlen = self._jsonheader_len
        if len(self._recv_buffer) >= hdrlen:
            self.jsonheader = self._json_decode(
                self._recv_buffer[:hdrlen], "utf-8"
            )
            self._recv_buffer = self._recv_buffer[hdrlen:]
            for reqhdr in (
                    "byteorder",
                    "content-length",
                    "content-type",
                    "content-encoding",
            ):
                if reqhdr not in self.jsonheader:
                    raise ValueError(f'Missing required header "{reqhdr}".')

    def process_request(self):
        '''
        Content of message; output self.request
        '''
        content_len = self.jsonheader["content-length"]
        if not len(self._recv_buffer) >= content_len:
            return
        data = self._recv_buffer[:content_len]
        if self.jsonheader["content-type"] == "text/json":
            encoding = self.jsonheader["content-encoding"]
            self.request = self._json_decode(data, encoding)
            if self.verbose:
                print("received request", repr(self.request), "from", self.addr)
        else:
            # Unknown content-type
            self.request = data
            print(
                f'received {self.jsonheader["content-type"]} request from',
                self.addr,
            )
            # set selector to listen for write events, we're done reading
            self._set_selector_events_mask("w")

    def create_response(self):
        '''
        creates response, some sort of acknowledge likely
        '''
        if self.jsonheader["content-type"] == "text/json":
            response = self._create_response_json_content()
        else:
            # This is will likely fail, and badly
            response = self._create_empty_response_content()
        message = self._create_message(**response)
        self.response_created = True
        self._send_buffer += message
