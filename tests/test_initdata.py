import base64
import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from change_windows.initdata import InitDataError, verify_ed25519, verify_hmac

BOT_ID = 123456789
NOW = 1_800_000_000
USER = json.dumps({"id": 42, "first_name": "Test"}, separators=(",", ":"))


def signed(fields, key, bot_id=BOT_ID):
    dcs = f"{bot_id}:WebAppData\n" + "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    sig = base64.urlsafe_b64encode(key.sign(dcs.encode())).decode().rstrip("=")
    return urlencode({**fields, "signature": sig, "hash": "ignored-by-ed25519"})


@pytest.fixture
def key():
    k = Ed25519PrivateKey.generate()
    pub = k.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    return k, pub


def test_ed25519_round_trip(key):
    k, pub = key
    data = signed({"auth_date": str(NOW - 10), "query_id": "AAE", "user": USER}, k)
    assert verify_ed25519(data, BOT_ID, public_key_hex=pub, now=NOW)["id"] == 42


@pytest.mark.parametrize("mutate", ["tamper", "bot", "stale", "future", "nosig"])
def test_ed25519_rejects(key, mutate):
    k, pub = key
    fields = {"auth_date": str(NOW - 10), "user": USER}
    bot = BOT_ID
    if mutate == "stale":
        fields["auth_date"] = str(NOW - 7200)
    if mutate == "future":
        fields["auth_date"] = str(NOW + 7200)
    data = signed(fields, k)
    if mutate == "tamper":
        data = data.replace("Test", "Evil")
    if mutate == "bot":
        bot = BOT_ID + 1
    if mutate == "nosig":
        data = urlencode(fields)
    with pytest.raises(InitDataError):
        verify_ed25519(data, bot, public_key_hex=pub, now=NOW)


def test_ed25519_rejects_wrong_key(key):
    k, _ = key
    data = signed({"auth_date": str(NOW), "user": USER}, k)
    with pytest.raises(InitDataError):
        verify_ed25519(data, BOT_ID, now=NOW)  # Telegram's production key, not ours


def test_duplicate_field_rejected(key):
    k, pub = key
    data = signed({"auth_date": str(NOW), "user": USER}, k) + "&user=%7B%7D"
    with pytest.raises(InitDataError):
        verify_ed25519(data, BOT_ID, public_key_hex=pub, now=NOW)


def test_hmac_round_trip_and_reject():
    token = "123456789:SYNTHETIC-NOT-A-REAL-TOKEN"
    fields = {"auth_date": str(NOW), "user": USER}
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    good = urlencode({**fields, "hash": hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()})
    assert verify_hmac(good, token, now=NOW)["id"] == 42
    with pytest.raises(InitDataError):
        verify_hmac(good, token + "x", now=NOW)
