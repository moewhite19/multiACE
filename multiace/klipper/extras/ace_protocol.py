class AceProtocol(object):
    NAME = ''
    DEFAULT_BAUD = 0
    SERIAL_KWARGS = {}

    @classmethod
    def discover(cls):
        raise NotImplementedError

    @classmethod
    def open_transport(cls, path, baud, **kwargs):
        import serial
        merged = dict(cls.SERIAL_KWARGS)
        merged.update(kwargs)
        return serial.Serial(port=path, baudrate=baud, **merged)

    def encode_request(self, request, next_id=None):
        raise NotImplementedError

    def decode_frames(self, buffer):
        raise NotImplementedError

    def initial_handshake_requests(self):
        return [{'method': 'get_info'}]

    def make_default_info(self):
        raise NotImplementedError
