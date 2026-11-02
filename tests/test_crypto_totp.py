import stat
from datetime import UTC, datetime, timedelta

import pyotp
import pytest

from mediawrangler.auth import passwords, totp
from mediawrangler.config import Settings
from mediawrangler.crypto import GENERATED_KEY_NAME, SecretBox, SecretDecryptError, load_master_key


def test_secret_round_trip():
    box = SecretBox(b"key-one")
    token = box.encrypt("api-key-123")
    assert "api-key-123" not in token
    assert box.decrypt(token) == "api-key-123"


def test_wrong_master_key_fails_loudly():
    token = SecretBox(b"key-one").encrypt("api-key-123")
    with pytest.raises(SecretDecryptError):
        SecretBox(b"key-two").decrypt(token)


def test_generated_master_key_is_private_and_stable(tmp_path):
    s = Settings(data_dir=tmp_path)
    first = load_master_key(s)
    path = tmp_path / GENERATED_KEY_NAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_master_key(s) == first


def test_key_file_takes_precedence(tmp_path):
    key_file = tmp_path / "k"
    key_file.write_text("from-file\n")
    s = Settings(data_dir=tmp_path, secret_key_file=key_file, secret_key="from-env")
    assert load_master_key(s) == b"from-file"


def test_password_policy():
    assert passwords.password_problem("short") is not None
    assert passwords.password_problem("a" * 9) is not None
    assert passwords.password_problem("a" * 10) is None
    assert passwords.password_problem(" " + "a" * 10) is not None
    assert passwords.password_problem("a" * 10, "b" * 10) == "Passwords do not match."


def test_password_hash_and_verify():
    h = passwords.hash_password("correct horse battery staple")
    assert passwords.verify_password(h, "correct horse battery staple")
    assert not passwords.verify_password(h, "wrong")
    assert not passwords.verify_password(None, "anything")


def test_totp_accepts_current_and_adjacent_steps():
    secret = totp.new_secret()
    now = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)
    gen = pyotp.TOTP(secret)
    assert totp.verify_code(secret, gen.at(now), now=now) == gen.timecode(now)
    assert totp.verify_code(secret, gen.at(now - timedelta(seconds=30)), now=now) is not None
    assert totp.verify_code(secret, gen.at(now - timedelta(seconds=90)), now=now) is None


def test_totp_rejects_replay():
    secret = totp.new_secret()
    now = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)
    code = pyotp.TOTP(secret).at(now)
    counter = totp.verify_code(secret, code, now=now)
    assert totp.verify_code(secret, code, last_counter=counter, now=now) is None


def test_totp_rejects_malformed():
    secret = totp.new_secret()
    assert totp.verify_code(secret, "12345") is None
    assert totp.verify_code(secret, "abcdef") is None


def test_recovery_codes():
    codes = totp.new_recovery_codes()
    assert len(codes) == totp.RECOVERY_CODE_COUNT
    assert len(set(codes)) == len(codes)
    h = totp.hash_recovery_code(codes[0])
    assert totp.recovery_code_matches(codes[0].upper().replace("-", " "), h)
    assert not totp.recovery_code_matches(codes[1], h)
    assert totp.looks_like_recovery_code(codes[0])
    assert not totp.looks_like_recovery_code("123 456")
