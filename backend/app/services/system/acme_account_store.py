"""Encrypted ACME account-key persistence with atomic first-writer selection."""

import hashlib
import os
import stat
import tempfile
from contextlib import suppress
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def load_or_create_account_key(directory: str, ca_url: str, encryption_key: str):
    """Retain one key per CA directory across processes without exposing it.

    Hard-link publication makes complete encrypted content visible atomically;
    concurrent losers reload the chosen key. Corrupt keys fail closed.
    """
    if not encryption_key:
        raise ValueError("acme_account_encryption_key_not_configured")
    fernet = Fernet(encryption_key.encode())
    root = Path(os.path.abspath(directory))
    if any(part.is_symlink() for part in (root, *root.parents)):
        raise ValueError("acme_account_directory_symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise ValueError("acme_account_directory_symlink")
    os.chmod(root, 0o700)
    path = root / (hashlib.sha256(ca_url.encode()).hexdigest() + ".key.enc")

    def read_key():
        if path.is_symlink():
            raise ValueError("acme_account_key_symlink")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                raise ValueError("acme_account_key_not_regular")
            payload = file.read(32769)
        if len(payload) > 32768:
            raise ValueError("acme_account_key_too_large")
        key = serialization.load_pem_private_key(fernet.decrypt(payload), password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise ValueError("acme_account_key_not_rsa")
        return key

    try:
        return read_key()
    except FileNotFoundError:
        pass
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    fd, temp = tempfile.mkstemp(prefix=".account-", dir=root)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(fernet.encrypt(pem))
            file.flush()
            os.fsync(file.fileno())
        with suppress(FileExistsError):
            os.link(temp, path)
        return read_key()
    finally:
        Path(temp).unlink(missing_ok=True)
