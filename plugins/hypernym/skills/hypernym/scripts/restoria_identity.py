"""RESTORIA identity for the hypernym plugin — standard library only. No secret of anyone else's, ever.

The plugin holds exactly two private keys, both on this machine, both at mode 600:
  ~/.hypernym/identity/principal.json   YOU: 12 Banksian words → your principal key (the same derivation
                                         as a Zephyr's node identity). One principal across every Zephyr.
                                         The words move your identity to another machine: keep them.
  ~/.hypernym/identity/device.json      THIS install's device key. Your principal authorizes it; it signs
                                         every request. Revocable on its own.

Crypto (byte-identical to the Zephyr's eth_account and RESTORIA's Rust, checked against both):
  Keccak-256 (Ethereum padding), secp256k1 ECDSA with RFC 6979 deterministic nonces and low-s,
  EIP-191 personal_sign over the canonical statement text, Ethereum addresses.

Canonical statements: the domain tag, then key=value lines joined by \\n, no trailing newline —
exactly the RESTORIA encoder. NO .get().
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

IDENTITY_DIR = Path.home() / '.hypernym' / 'identity'
PRINCIPAL_FILE = IDENTITY_DIR / 'principal.json'
DEVICE_FILE = IDENTITY_DIR / 'device.json'
WORDLIST = Path(__file__).resolve().parent / 'restoria_banksian_seed_wordlist.json'
IDENTITY_DOMAIN = b'restoria-zephyr-node-identity-v1'
WORD_COUNT = 12
CREDENTIAL_PREFIX = 'restoria1.'

# ---------------------------------------------------------------- Keccak-256

_RC = [0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
       0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
       0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
       0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
       0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
       0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008]
_ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61], [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
_M64 = (1 << 64) - 1


def _rol(x: int, n: int) -> int:
    if n == 0:
        return x
    return ((x << n) | (x >> (64 - n))) & _M64


def _keccak_f(a: list) -> list:
    for rc in _RC:
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        a = [[a[x][y] ^ d[x] for y in range(5)] for x in range(5)]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rol(a[x][y], _ROT[x][y])
        a = [[b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y]) for y in range(5)] for x in range(5)]
        a[0][0] ^= rc
    return a


def keccak256(data: bytes) -> bytes:
    rate = 136
    padded = bytearray(data) + b'\x01'
    padded += b'\x00' * ((-len(padded)) % rate)
    padded[-1] |= 0x80
    a = [[0] * 5 for _ in range(5)]
    for off in range(0, len(padded), rate):
        for i in range(rate // 8):
            a[i % 5][i // 5] ^= int.from_bytes(padded[off + 8 * i:off + 8 * i + 8], 'little')
        a = _keccak_f(a)
    return b''.join(a[i % 5][i // 5].to_bytes(8, 'little') for i in range(4))


# ---------------------------------------------------------------- secp256k1

_P = 2 ** 256 - 2 ** 32 - 977
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
      0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _jdouble(p):
    x, y, z = p
    if y == 0:
        return (0, 0, 0)
    s = 4 * x * y * y % _P
    m = 3 * x * x % _P
    nx = (m * m - 2 * s) % _P
    return (nx, (m * (s - nx) - 8 * y ** 4) % _P, 2 * y * z % _P)


def _jadd(p, q):
    if p[2] == 0:
        return q
    if q[2] == 0:
        return p
    x1, y1, z1 = p
    x2, y2, z2 = q
    u1 = x1 * z2 * z2 % _P
    u2 = x2 * z1 * z1 % _P
    s1 = y1 * z2 ** 3 % _P
    s2 = y2 * z1 ** 3 % _P
    if u1 == u2:
        if s1 != s2:
            return (0, 0, 0)
        return _jdouble(p)
    h = u2 - u1
    r = s2 - s1
    h2 = h * h % _P
    h3 = h * h2 % _P
    u1h2 = u1 * h2 % _P
    nx = (r * r - h3 - 2 * u1h2) % _P
    return (nx, (r * (u1h2 - nx) - s1 * h3) % _P, h * z1 * z2 % _P)


def _mul(k: int):
    r = (0, 0, 0)
    q = (_G[0], _G[1], 1)
    while k:
        if k & 1:
            r = _jadd(r, q)
        q = _jdouble(q)
        k >>= 1
    zi = pow(r[2], -1, _P)
    return (r[0] * zi * zi % _P, r[1] * zi ** 3 % _P)


def address_of(private_key: int) -> str:
    x, y = _mul(private_key)
    return '0x' + keccak256(x.to_bytes(32, 'big') + y.to_bytes(32, 'big'))[-20:].hex()


def _rfc6979_nonce(private_key: int, digest: bytes) -> int:
    x = private_key.to_bytes(32, 'big')
    v = b'\x01' * 32
    k = b'\x00' * 32
    k = hmac.new(k, v + b'\x00' + x + digest, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b'\x01' + x + digest, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        candidate = int.from_bytes(v, 'big')
        if 1 <= candidate < N:
            return candidate
        k = hmac.new(k, v + b'\x00', hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


def sign_text(private_key: int, text: str) -> str:
    """EIP-191 personal_sign over `text`: 65 bytes r‖s‖v (v = 27/28, low-s), as hex."""
    message = text.encode('utf-8')
    digest = keccak256(b'\x19Ethereum Signed Message:\n' + str(len(message)).encode() + message)
    z = int.from_bytes(digest, 'big')
    k = _rfc6979_nonce(private_key, digest)
    rx, ry = _mul(k)
    r = rx % N
    s = pow(k, -1, N) * (z + r * private_key) % N
    v = ry & 1
    if s > N // 2:
        s = N - s
        v ^= 1
    return (r.to_bytes(32, 'big') + s.to_bytes(32, 'big') + bytes([27 + v])).hex()


# ---------------------------------------------------------------- canonical statements

def canonical_text(domain_tag: str, fields: list) -> str:
    """Domain tag, then key=value lines in the given order, \\n-joined, no trailing newline."""
    lines = [domain_tag]
    for key, value in fields:
        if '\n' in value:
            raise ValueError(f"line break in {key}")
        if '\r' in value:
            raise ValueError(f"line break in {key}")
        lines.append(f"{key}={value}")
    return '\n'.join(lines)


def device_authorization_fields(device: str, principal: str, scopes: list, label: str,
                                authorized_at_micros: int, expires_at_micros: int) -> dict:
    return {'device': device, 'principal': principal, 'scopes': ','.join(sorted(scopes)), 'label': label,
            'authorized_at': str(authorized_at_micros), 'expires_at': str(expires_at_micros)}


def device_authorization_text(fields: dict) -> str:
    return canonical_text('RESTORIA-DEVICE-AUTHORIZATION-v1', [
        ('device', fields['device']), ('principal', fields['principal']), ('scopes', fields['scopes']),
        ('label', fields['label']), ('authorized_at', fields['authorized_at']),
        ('expires_at', fields['expires_at'])])


def request_credential(device_key: int, device: str, audience: str, method: str, target: str, body: bytes) -> str:
    """The X-API-Key value for one request: restoria1.<base64url(statement)>.<hex signature>.
    `target` is the path + query exactly as sent on the wire."""
    text = canonical_text('RESTORIA-REQUEST-v1', [
        ('device', device), ('audience', audience.lower()), ('method', method.upper()), ('path', target),
        ('body_sha256', hashlib.sha256(body).hexdigest()), ('issued_at', str(int(time.time() * 1_000_000))),
        ('nonce', secrets.token_hex(16))])
    encoded = base64.urlsafe_b64encode(text.encode('utf-8')).decode().rstrip('=')
    return f"{CREDENTIAL_PREFIX}{encoded}.{sign_text(device_key, text)}"


def expected_enrollment_text(principal: str, node: str, statement: str) -> bool:
    """Before the principal signs what the node sends back: it must be an enrollment of THIS principal
    attested by THE node we enrolled with — nothing else ever gets the principal's signature."""
    lines = statement.split('\n')
    if lines[0] != 'RESTORIA-PRINCIPAL-ENROLLMENT-v1':
        return False
    if len(lines) != 5:
        return False
    if lines[1] != f"principal={principal}":
        return False
    if not lines[2].startswith('github_user_id='):
        return False
    return lines[3] == f"attesting_node={node.lower()}"


# ---------------------------------------------------------------- key files

def _load_words() -> list:
    table = json.loads(WORDLIST.read_text())
    words = table['words']
    if len(words) != table['count']:
        raise ValueError("Banksian word table is malformed")
    return words


def _key_from_words(words: list) -> int:
    table = _load_words()
    index = {w: i for i, w in enumerate(table)}
    if len(words) != WORD_COUNT:
        raise ValueError(f"a principal is exactly {WORD_COUNT} words, got {len(words)}")
    value = 0
    for w in words:
        if w.lower() not in index:
            raise ValueError(f"not a Banksian word: {w!r}")
        value = (value << 10) | index[w.lower()]
    key = int.from_bytes(hashlib.sha256(IDENTITY_DOMAIN + value.to_bytes(15, 'big')).digest(), 'big')
    if not 1 <= key < N:
        raise ValueError("these words derive an invalid key; choose other words")
    return key


def _write_secret(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    old = os.umask(0o077)
    try:
        path.write_text(json.dumps(obj, indent=1))
    finally:
        os.umask(old)
    os.chmod(path, 0o600)


def load_or_create_principal(words_text: str = '') -> tuple:
    """(private_key, address, created_now, words). From the saved file, from given words, or freshly minted."""
    if PRINCIPAL_FILE.exists():
        saved = json.loads(PRINCIPAL_FILE.read_text())
        key = _key_from_words(saved['words'])
        if words_text:
            given = _key_from_words(words_text.split())
            if given != key:
                raise ValueError(f"{PRINCIPAL_FILE} already holds a different principal; move it aside first")
        return key, address_of(key), False, saved['words']
    if words_text:
        words = [w.lower() for w in words_text.split()]
    else:
        table = _load_words()
        value = int.from_bytes(secrets.token_bytes(15), 'big')
        words = []
        for _ in range(WORD_COUNT):
            words.append(table[value & 1023])
            value >>= 10
        words.reverse()
    key = _key_from_words(words)
    address = address_of(key)
    _write_secret(PRINCIPAL_FILE, {'address': address, 'words': words})
    return key, address, True, words


def load_or_create_device() -> tuple:
    """(private_key, address). One per install."""
    if DEVICE_FILE.exists():
        saved = json.loads(DEVICE_FILE.read_text())
        key = int(saved['private_key'], 16)
        return key, address_of(key)
    key = 0
    while not 1 <= key < N:
        key = int.from_bytes(secrets.token_bytes(32), 'big')
    address = address_of(key)
    _write_secret(DEVICE_FILE, {'address': address, 'private_key': format(key, '064x')})
    return key, address


def load_device() -> tuple:
    """(private_key, address) of this install's device key; raises if none exists (not signed in)."""
    saved = json.loads(DEVICE_FILE.read_text())
    key = int(saved['private_key'], 16)
    return key, address_of(key)
