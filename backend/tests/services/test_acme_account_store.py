"""Behavioral ACME account persistence against real files and child processes."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.services.system.acme_account_store import load_or_create_account_key

pytestmark = pytest.mark.behavioral
PRODUCTION_CA = "https://acme-v02.api.letsencrypt.org/directory"
STAGING_CA = "https://acme-staging-v02.api.letsencrypt.org/directory"
BACKEND = Path(__file__).resolve().parents[2]


def public_bytes(key):
    return key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def account_path(root, ca=PRODUCTION_CA):
    return root / (hashlib.sha256(ca.encode()).hexdigest() + ".key.enc")


def child(root, encryption_key, ca=PRODUCTION_CA):
    # Only synthetic test credentials enter the child environment. Output is a
    # public-key digest; private PEM and encryption keys never reach stdout.
    script = """
import hashlib, os, sys
from cryptography.hazmat.primitives import serialization
from app.services.system.acme_account_store import load_or_create_account_key
key = load_or_create_account_key(sys.argv[1], sys.argv[2], os.environ['NOVUSAI_TEST_ACCOUNT_KEY'])
pub = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
print(hashlib.sha256(pub).hexdigest())
"""
    env = {
        **os.environ,
        "NOVUSAI_TEST_ACCOUNT_KEY": encryption_key,
        "NOVUSAI_CLI_DISABLE_FILE_LOGGING": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return subprocess.Popen(
        [sys.executable, "-c", script, str(root), ca],
        cwd=BACKEND,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def result(process):
    try:
        out, err = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        pytest.fail("Disposable account-store child timed out")
    assert process.returncode == 0 and err == ""
    digest = out.strip()
    assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
    return digest


def test_account_key_survives_process_restart_and_is_encrypted(tmp_path):
    root = tmp_path / "accounts"
    encryption_key = Fernet.generate_key().decode()
    created = load_or_create_account_key(str(root), PRODUCTION_CA, encryption_key)
    original = account_path(root).read_bytes()
    private_pem = Fernet(encryption_key.encode()).decrypt(original)
    parsed = serialization.load_pem_private_key(private_pem, password=None)
    assert public_bytes(parsed) == public_bytes(created)
    assert b"PRIVATE KEY" not in original and private_pem not in original
    assert (
        result(child(root, encryption_key))
        == hashlib.sha256(public_bytes(created)).hexdigest()
    )
    assert account_path(root).read_bytes() == original
    assert sorted(p.name for p in root.iterdir()) == [account_path(root).name]


def test_concurrent_processes_choose_one_complete_account_key(tmp_path):
    root = tmp_path / "accounts"
    encryption_key = Fernet.generate_key().decode()
    processes = [child(root, encryption_key) for _ in range(4)]
    try:
        digests = [result(process) for process in processes]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()
    selected = load_or_create_account_key(str(root), PRODUCTION_CA, encryption_key)
    assert digests == [hashlib.sha256(public_bytes(selected)).hexdigest()] * 4
    assert list(root.iterdir()) == [account_path(root)]


def test_staging_and_production_have_distinct_persistent_accounts(tmp_path):
    encryption_key = Fernet.generate_key().decode()
    production = load_or_create_account_key(
        str(tmp_path), PRODUCTION_CA, encryption_key
    )
    staging = load_or_create_account_key(str(tmp_path), STAGING_CA, encryption_key)
    assert public_bytes(production) != public_bytes(staging)
    assert public_bytes(
        load_or_create_account_key(str(tmp_path), PRODUCTION_CA, encryption_key)
    ) == public_bytes(production)
    assert public_bytes(
        load_or_create_account_key(str(tmp_path), STAGING_CA, encryption_key)
    ) == public_bytes(staging)
    assert {p.name for p in tmp_path.iterdir()} == {
        account_path(tmp_path).name,
        account_path(tmp_path, STAGING_CA).name,
    }


@pytest.mark.parametrize(
    "kind", ["missing", "malformed", "wrong", "corrupt", "oversized", "non_rsa"]
)
def test_invalid_keys_fail_closed_without_replacing_account(tmp_path, kind):
    encryption_key = Fernet.generate_key().decode()
    load_or_create_account_key(str(tmp_path), PRODUCTION_CA, encryption_key)
    path = account_path(tmp_path)
    supplied = encryption_key
    if kind == "missing":
        supplied = ""
    elif kind == "malformed":
        supplied = "invalid-fernet-key"
    elif kind == "wrong":
        supplied = Fernet.generate_key().decode()
    elif kind == "corrupt":
        path.write_bytes(b"corrupt-encrypted-account")
    elif kind == "oversized":
        path.write_bytes(b"x" * 32769)
    else:
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        path.write_bytes(Fernet(encryption_key.encode()).encrypt(pem))
    previous = path.read_bytes()
    with pytest.raises((ValueError, InvalidToken)):
        load_or_create_account_key(str(tmp_path), PRODUCTION_CA, supplied)
    assert path.read_bytes() == previous
    assert list(tmp_path.iterdir()) == [path]


def create_symlink(link, target, *, is_directory=False):
    try:
        link.symlink_to(target, target_is_directory=is_directory)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows runner lacks symlink creation privilege")
        raise


@pytest.mark.parametrize("location", ["root", "key", "parent"])
def test_symlink_paths_fail_closed_without_touching_external_target(tmp_path, location):
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel"
    sentinel.write_bytes(b"external-data")
    root = tmp_path / "accounts"
    encryption_key = Fernet.generate_key().decode()
    if location == "root":
        create_symlink(root, external, is_directory=True)
    elif location == "parent":
        parent = tmp_path / "linked-parent"
        create_symlink(parent, external, is_directory=True)
        root = parent / "accounts"
    else:
        root.mkdir()
        create_symlink(account_path(root), sentinel)
    with pytest.raises(ValueError, match="symlink"):
        load_or_create_account_key(str(root), PRODUCTION_CA, encryption_key)
    assert sentinel.read_bytes() == b"external-data"
    assert {p.name for p in external.iterdir()} == {"sentinel"}


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX permission bits do not prove Windows ACLs"
)
def test_account_store_directory_0700_file_0600(tmp_path):
    root = tmp_path / "accounts"
    load_or_create_account_key(str(root), PRODUCTION_CA, Fernet.generate_key().decode())
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(account_path(root).stat().st_mode) == 0o600
