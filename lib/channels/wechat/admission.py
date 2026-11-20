"""Bind iLink requests to the shared transport's final wire admission."""
from lib.http_transport.outbound import current_wire_send_authority, wire_send_authority


class RefusedBeforeWire(Exception):
    def __init__(self, receipt):
        super().__init__('微信 request refused before wire admission')
        self.receipt = receipt


def request(send, method, url, *, authorize=None, **kwargs):
    if authorize is None:
        return send(method, url, **kwargs)

    outer = current_wire_send_authority()

    def guard():
        if outer is not None:
            outer()
        refusal = authorize()
        if refusal is not None:
            raise RefusedBeforeWire(refusal)

    with wire_send_authority(guard):
        return send(method, url, **kwargs)
