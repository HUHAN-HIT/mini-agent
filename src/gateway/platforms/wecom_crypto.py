"""WeCom (企业微信) callback crypto: signature verify + AES-CBC decrypt + XML parse.

This is a compact, dependency-light re-implementation of the official
WXBizMsgCrypt sample distributed by Tencent. The algorithm is documented in
the WeCom open API reference and is reproduced here from the public spec.

Notes on safety:

- XML is parsed with ``defusedxml`` to block XXE and external entity attacks.
- Random padding follows PKCS7 with block size 32 (WeCom's choice, not AES's 16).
- The 16-byte IV is the first AES block of the cipher text, per WeCom.

The class deliberately raises a single ``WeComCryptoError`` so callers can
treat any signature/decrypt failure uniformly as "this app didn't produce the
payload" and move on to the next configured app.
"""

from __future__ import annotations

import base64
import hashlib
import socket
import struct
from dataclasses import dataclass
from typing import Optional

try:
    from Crypto.Cipher import AES  # pycryptodome
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("pycryptodome is required for wecom crypto (pip install pycryptodome)") from exc

try:
    from defusedxml import ElementTree as DET  # type: ignore
except ImportError:  # pragma: no cover - fall back to stdlib for tests
    from xml.etree import ElementTree as DET  # type: ignore


class WeComCryptoError(Exception):
    """Raised when signature verification or decryption fails."""


@dataclass
class WeComMessage:
    msg_id: str
    msg_type: str
    event: str
    from_user: str
    to_user: str
    content: str
    raw_xml: str
    agent_id: str = ""


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    pad_len = data[-1]
    if pad_len < 1 or pad_len > 32:
        raise WeComCryptoError("invalid PKCS7 padding")
    if data[-pad_len:] != bytes([pad_len]) * pad_len:
        raise WeComCryptoError("invalid PKCS7 padding bytes")
    return data[:-pad_len]


class WeComCryptor:
    """Signature verify + AES-CBC-256 decrypt for one WeCom app."""

    def __init__(self, *, corp_id: str, token: str, encoding_aes_key: str) -> None:
        if not corp_id or not token or not encoding_aes_key:
            raise WeComCryptoError("corp_id/token/encoding_aes_key are all required")
        if len(encoding_aes_key) != 43:
            raise WeComCryptoError("encoding_aes_key must be 43 chars")
        self.corp_id = corp_id
        self.token = token
        try:
            self.aes_key = base64.b64decode(encoding_aes_key + "=")
        except Exception as exc:
            raise WeComCryptoError(f"bad encoding_aes_key: {exc}") from exc

    def verify_signature(self, *, signature: str, timestamp: str, nonce: str, encrypt: str) -> None:
        parts = sorted([self.token, timestamp, nonce, encrypt])
        computed = hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()
        if computed != signature:
            raise WeComCryptoError("signature mismatch")

    def verify_url(self, *, signature: str, timestamp: str, nonce: str, echostr: str) -> str:
        self.verify_signature(signature=signature, timestamp=timestamp, nonce=nonce, encrypt=echostr)
        plain = self._decrypt(echostr)
        return plain.decode("utf-8", errors="replace")

    def decrypt_payload(self, *, signature: str, timestamp: str, nonce: str, body: bytes) -> str:
        """Verify + decrypt a callback POST body and return the plaintext XML."""

        xml_root = DET.fromstring(body.decode("utf-8", errors="replace"))
        encrypt_elem = xml_root.find("Encrypt")
        if encrypt_elem is None or not encrypt_elem.text:
            raise WeComCryptoError("missing <Encrypt> in body")
        encrypt = encrypt_elem.text.strip()
        self.verify_signature(signature=signature, timestamp=timestamp, nonce=nonce, encrypt=encrypt)
        plain = self._decrypt(encrypt)
        return plain.decode("utf-8", errors="replace")

    def _decrypt(self, encrypt_b64: str) -> bytes:
        try:
            cipher_text = base64.b64decode(encrypt_b64)
        except Exception as exc:
            raise WeComCryptoError(f"bad base64 payload: {exc}") from exc
        if len(cipher_text) < 48 or len(cipher_text) % 32 != 0:
            raise WeComCryptoError("ciphertext length invalid")

        iv = cipher_text[:16]
        cipher = AES.new(self.aes_key, AES.MODE_CBC, iv)
        plain = cipher.decrypt(cipher_text[16:])
        plain = _pkcs7_unpad(plain)

        # plain = random(16) + msg_len(4, network order) + msg_body + appid
        if len(plain) < 20:
            raise WeComCryptoError("decrypted payload too short")
        msg_len = socket.ntohl(struct.unpack("I", plain[16:20])[0])
        if 20 + msg_len > len(plain):
            raise WeComCryptoError("decrypted msg_len out of bounds")
        msg_body = plain[20:20 + msg_len]
        from_id = plain[20 + msg_len:].decode("utf-8", errors="replace")
        if from_id != self.corp_id:
            raise WeComCryptoError(f"appid mismatch: expected {self.corp_id}, got {from_id}")
        return msg_body


def parse_wecom_xml(xml_text: str) -> Optional[WeComMessage]:
    """Parse a decrypted WeCom callback XML into a typed message.

    Returns None for events the gateway explicitly ignores at the routing
    layer (currently ``enter_agent`` and ``subscribe``).
    """

    if not xml_text:
        return None
    root = DET.fromstring(xml_text)
    msg_type = (root.findtext("MsgType") or "").strip().lower()
    event = (root.findtext("Event") or "").strip().lower()

    if msg_type == "event" and event in {"enter_agent", "subscribe"}:
        return None
    if msg_type and msg_type not in {"text", "event", "image", "voice", "video", "location", "link"}:
        return None

    return WeComMessage(
        msg_id=(root.findtext("MsgId") or "").strip(),
        msg_type=msg_type,
        event=event,
        from_user=(root.findtext("FromUserName") or "").strip(),
        to_user=(root.findtext("ToUserName") or "").strip(),
        content=(root.findtext("Content") or "").strip(),
        agent_id=(root.findtext("AgentID") or "").strip(),
        raw_xml=xml_text,
    )
