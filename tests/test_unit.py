"""tools/server.py 纯逻辑单元测试：票据核销 / 配置容错 / XFF（不触网络）。"""
import base64
import time
from types import SimpleNamespace

import server
from conftest import make_ticket


# --- _ticket_verify / _ticket_mark_used ---------------------------------------

def test_ticket_old_3part_format_valid():
    ok, identity, pending = server._ticket_verify(make_ticket())
    assert ok is True
    assert identity is None
    assert pending is not None


def test_ticket_new_4part_format_extracts_identity():
    ticket = make_ticket(identity="acct:user-42")
    ok, identity, pending = server._ticket_verify(ticket)
    assert ok is True
    assert identity == "acct:user-42"
    assert pending == ("jti-1", int(ticket.split(".")[0]))


def test_ticket_guest_identity():
    ok, identity, _ = server._ticket_verify(make_ticket(identity="guest:abc123"))
    assert ok is True
    assert identity == "guest:abc123"


def test_ticket_bad_signature_rejected():
    ticket = make_ticket(identity="acct:u1")
    parts = ticket.split(".")
    parts[-1] = "0" * 32
    ok, identity, pending = server._ticket_verify(".".join(parts))
    assert ok is False
    assert identity is None
    assert pending is None


def test_ticket_non_ascii_sig_rejected_not_500():
    # 非 ASCII 签名：旧实现 hmac.compare_digest 抛 TypeError → 握手 500；
    # 现必须安静拒收（4401 由 realtime()  close code 表达）
    ticket = make_ticket(identity="acct:u1")
    parts = ticket.split(".")
    parts[-1] = "签" * 16
    assert not parts[-1].isascii()
    ok, identity, pending = server._ticket_verify(".".join(parts))
    assert ok is False
    assert identity is None
    assert pending is None


def test_ticket_non_hex_ascii_sig_rejected():
    # ASCII 但非 32 位小写 hex（错长度/含字母表外字符）同样前置拒收
    ticket = make_ticket(identity="acct:u1")
    parts = ticket.split(".")
    parts[-1] = "z" * 32
    assert server._ticket_verify(".".join(parts))[0] is False
    parts[-1] = "a" * 31
    assert server._ticket_verify(".".join(parts))[0] is False


def test_ticket_wrong_secret_rejected():
    ok, _, _ = server._ticket_verify(make_ticket(secret="other-secret"))
    assert ok is False


def test_ticket_expired_rejected():
    ok, _, _ = server._ticket_verify(make_ticket(exp=int(time.time()) - 1))
    assert ok is False


def test_ticket_malformed_rejected():
    assert server._ticket_verify("")[0] is False
    assert server._ticket_verify("a.b")[0] is False
    assert server._ticket_verify("a.b.c.d.e")[0] is False
    assert server._ticket_verify("notanint.jti.sig")[0] is False


def test_ticket_empty_jti_rejected():
    exp = int(time.time()) + 60
    assert server._ticket_verify("%d..%s" % (exp, "0" * 32))[0] is False


def test_ticket_invalid_idb64_rejected():
    # 签名合法但 idb64 解不出身份 → 拒绝（构造：正常票据换掉 idb64 后重签）
    exp = int(time.time()) + 60
    jti = "jti-badid"
    idb64 = "a"  # 长度 %4==1，补 padding 也无法解码
    import hashlib
    import hmac
    signed = "asr-realtime.%d.%s.%s" % (exp, jti, idb64)
    sig = hmac.new(server.TICKET_SECRET.encode(),
                   signed.encode(), hashlib.sha256).hexdigest()[:32]
    ok, _, _ = server._ticket_verify("%d.%s.%s.%s" % (exp, jti, idb64, sig))
    assert ok is False


def test_ticket_verify_does_not_consume():
    # 验签本身不核销：同一票据在未 mark_used 前可重复通过查重
    ticket = make_ticket(jti="jti-pending")
    assert server._ticket_verify(ticket)[0] is True
    assert server._ticket_verify(ticket)[0] is True
    assert "jti-pending" not in server._used_tickets


def test_ticket_jti_single_use_after_mark_used():
    # 核销（accept 后由门面调用 _ticket_mark_used）后 60s 窗口内重放必须被拒
    ticket = make_ticket(jti="jti-once")
    ok, _, pending = server._ticket_verify(ticket)
    assert ok is True
    server._ticket_mark_used(*pending)
    assert server._ticket_verify(ticket)[0] is False


def test_ticket_distinct_jti_both_valid():
    assert server._ticket_verify(make_ticket(jti="jti-a"))[0] is True
    assert server._ticket_verify(make_ticket(jti="jti-b"))[0] is True


def test_ticket_verify_purges_expired_used_jti():
    ticket = make_ticket(jti="jti-old", exp=int(time.time()) + 1)
    ok, _, pending = server._ticket_verify(ticket)
    assert ok is True
    server._ticket_mark_used(*pending)
    server._used_tickets["jti-old"] = int(time.time()) - 1  # 模拟已过期
    server._ticket_verify(make_ticket(jti="jti-new"))
    assert "jti-old" not in server._used_tickets


def test_ticket_no_secret_rejects(monkeypatch):
    monkeypatch.setattr(server, "TICKET_SECRET", "")
    ok, _, _ = server._ticket_verify(make_ticket())
    assert ok is False


# --- _getenv_int / _getenv_float ---------------------------------------------

def test_getenv_int_valid(monkeypatch):
    monkeypatch.setenv("CHII_ASR_FOO", "42")
    assert server._getenv_int("FOO", 7) == 42


def test_getenv_int_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("CHII_ASR_FOO", "abc")
    assert server._getenv_int("FOO", 7) == 7


def test_getenv_int_empty_falls_back(monkeypatch):
    monkeypatch.delenv("CHII_ASR_FOO", raising=False)
    assert server._getenv_int("FOO", 7) == 7


def test_getenv_float_valid(monkeypatch):
    monkeypatch.setenv("CHII_ASR_BAR", "2.5")
    assert server._getenv_float("BAR", 1.0) == 2.5


def test_getenv_float_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("CHII_ASR_BAR", "1.5x")
    assert server._getenv_float("BAR", 1.0) == 1.0


# --- _client_ip ----------------------------------------------------------------

def test_client_ip_loopback_trusts_xff_last_hop():
    headers = {"x-forwarded-for": "1.1.1.1, 2.2.2.2, 203.0.113.9"}
    assert server._client_ip("127.0.0.1", headers) == "203.0.113.9"


def test_client_ip_loopback_without_xff():
    assert server._client_ip("127.0.0.1", {}) == "127.0.0.1"


def test_client_ip_direct_ignores_xff():
    headers = {"x-forwarded-for": "203.0.113.9"}
    assert server._client_ip("198.51.100.7", headers) == "198.51.100.7"


# --- 鉴权 helper -----------------------------------------------------------------

def test_authorized_bearer_only():
    ok_req = SimpleNamespace(headers={"Authorization": "Bearer test-asr-key"})
    assert server._authorized(ok_req) is True
    no_header = SimpleNamespace(headers={})
    assert server._authorized(no_header) is False
    basic = SimpleNamespace(headers={"Authorization": "Basic test-asr-key"})
    assert server._authorized(basic) is False
    wrong = SimpleNamespace(headers={"Authorization": "Bearer wrong"})
    assert server._authorized(wrong) is False


def test_sanitize_filename():
    assert server._sanitize_filename("../../etc/passwd") == "passwd"
    assert server._sanitize_filename("a b/c;rm -rf.wav") == "c_rm_-rf.wav"
    assert server._sanitize_filename("") == "audio.bin"
    assert server._sanitize_filename("...") == "audio.bin"


# --- 深检探测音频 ---------------------------------------------------------------

def test_probe_wav_is_valid_short_16k_mono_wav():
    # 内置探测音频必须是合法、极短的 16kHz 16bit 单声道 WAV（深检走引擎批量转写路径）
    import io
    import wave
    with wave.open(io.BytesIO(server.PROBE_WAV), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == 16000
        assert 0 < w.getnframes() <= 16000  # ≤1s
