"""Encryption-at-rest for stored OAuth tokens -- stdlib only, no extra deps.

FirstBack ships dependency-light (requests + stdlib), so we can't reach for
`cryptography`/Fernet. Instead we compose well-vetted stdlib primitives into a
standard authenticated-encryption scheme:

  * Key separation: HKDF-SHA256 (RFC 5869) stretches the configured key into two
    independent subkeys -- one for the keystream, one for the MAC -- so the same
    master key is never used for two purposes.
  * Confidentiality: a SHA-256 keystream in counter mode (the hash used as a PRF)
    XORed with the plaintext. Each message uses a fresh random 16-byte nonce, so
    the keystream never repeats.
  * Integrity/authenticity: HMAC-SHA256 over nonce||ciphertext, encrypt-then-MAC.
    A wrong key or any tampering fails the MAC and decryption refuses to return
    garbage.

On-the-wire format of an encrypted value (all ASCII, safe for a TEXT column):

    enc:v1:<base64url( nonce[16] || ciphertext || mac[32] )>

The "enc:v1:" marker is what makes dual-read possible: a stored value that does
NOT start with the marker is treated as legacy plaintext and returned as-is. So an
account connected before encryption was switched on keeps working, and the first
refresh re-stores its tokens encrypted.

Key source: config.TOKEN_ENC_KEY (FIRSTBACK_TOKEN_KEY env). When it's empty,
encryption is a SAFE NO-OP -- encrypt() returns the plaintext unchanged -- so local
dev and a first boot without the key still work. decrypt() always handles both
forms regardless of whether a key is set.
"""
import base64
import hashlib
import hmac
import secrets as _secrets

from config import TOKEN_ENC_KEY

_MARKER = "enc:v1:"
_NONCE_LEN = 16
_MAC_LEN = 32
_INFO_ENC = b"firstback-token-enc/v1"
_INFO_MAC = b"firstback-token-mac/v1"


def enabled():
    """True when a token-encryption key is configured (new writes are encrypted)."""
    return bool(TOKEN_ENC_KEY)


def is_encrypted(value):
    """True if `value` is one of our encrypted blobs (vs legacy plaintext/None)."""
    return isinstance(value, str) and value.startswith(_MARKER)


def _hkdf(key_material, info, length=32):
    """HKDF-SHA256 (RFC 5869) with a fixed all-zero salt. One round is enough: we
    only ever need 32 bytes (== one SHA-256 block) per subkey."""
    salt = b"\x00" * hashlib.sha256().digest_size
    prk = hmac.new(salt, key_material, hashlib.sha256).digest()
    okm = hmac.new(prk, info + b"\x01", hashlib.sha256).digest()
    return okm[:length]


def _keystream(enc_key, nonce, nbytes):
    """SHA-256 in counter mode: HASH(enc_key || nonce || counter) blocks, truncated
    to `nbytes`. A standard hash-as-PRF stream cipher."""
    out = bytearray()
    counter = 0
    while len(out) < nbytes:
        block = hashlib.sha256(enc_key + nonce + counter.to_bytes(8, "big")).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:nbytes])


def encrypt(plaintext):
    """Encrypt a token string for storage. Returns None unchanged (so clearing a
    token stays a clear), and returns the plaintext unchanged when no key is set
    (safe no-op for local dev)."""
    if plaintext is None:
        return None
    if not TOKEN_ENC_KEY:
        return plaintext
    key_material = TOKEN_ENC_KEY.encode("utf-8")
    enc_key = _hkdf(key_material, _INFO_ENC)
    mac_key = _hkdf(key_material, _INFO_MAC)
    nonce = _secrets.token_bytes(_NONCE_LEN)
    data = plaintext.encode("utf-8")
    ct = bytes(a ^ b for a, b in zip(data, _keystream(enc_key, nonce, len(data))))
    mac = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
    blob = base64.urlsafe_b64encode(nonce + ct + mac).decode("ascii")
    return _MARKER + blob


def decrypt(value):
    """Decrypt a stored token. Dual-read:
      * None            -> None
      * legacy plaintext (no marker) -> returned unchanged
      * encrypted blob  -> decrypted, or None if the key is missing/wrong or the
        MAC fails (never returns corrupted plaintext; the caller then treats the
        business as needing a reconnect).
    """
    if value is None:
        return None
    if not is_encrypted(value):
        return value  # legacy plaintext row
    if not TOKEN_ENC_KEY:
        return None  # encrypted at rest but no key available to read it
    try:
        raw = base64.urlsafe_b64decode(value[len(_MARKER):].encode("ascii"))
    except (ValueError, TypeError):
        return None
    if len(raw) < _NONCE_LEN + _MAC_LEN:
        return None
    nonce = raw[:_NONCE_LEN]
    mac = raw[-_MAC_LEN:]
    ct = raw[_NONCE_LEN:-_MAC_LEN]
    key_material = TOKEN_ENC_KEY.encode("utf-8")
    mac_key = _hkdf(key_material, _INFO_MAC)
    expected = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        return None  # wrong key or tampered
    enc_key = _hkdf(key_material, _INFO_ENC)
    data = bytes(a ^ b for a, b in zip(ct, _keystream(enc_key, nonce, len(ct))))
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None
