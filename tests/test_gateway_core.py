"""Pure-function tests for the gateway core (no live network, no fixtures).

These cover the contracts the runner depends on: deterministic session keys,
capability-driven delivery chunking, TTL dedup, and the platform lock identity
rules. Adapter end-to-end tests belong in their own modules (P1a fixture work).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Allow running this file from anywhere by ensuring the project root is importable.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.gateway.base import (  # noqa: E402
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    PlatformCapabilities,
    SendResult,
    SessionSource,
)
from src.gateway.delivery import deliver_final_response, split_message  # noqa: E402
from src.gateway.platforms._utils import (  # noqa: E402
    MessageDeduplicator,
    RateLimitCircuit,
    TtlSet,
    hash_token,
)
from src.gateway.platforms.ilink_protocol import (  # noqa: E402
    IlinkRateLimited,
    IlinkSessionExpired,
    check_errcode,
    content_dedup_key,
    extract_messages,
    extract_text,
    get_updates_buf,
    guess_chat_type,
    normalize_message,
)
from src.gateway.session_key import build_session_key  # noqa: E402
from src.gateway.turn_queue import SessionTurnQueue  # noqa: E402


# ---------------------------------------------------------------------------
# session_key
# ---------------------------------------------------------------------------


def _dm(platform="wecom", account="default", chat_id="corp1:userA", user_id="userA"):
    return SessionSource(
        platform=platform,
        chat_id=chat_id,
        chat_type="dm",
        user_id=user_id,
        account_id=account,
        raw_chat_id=chat_id,
    )


def test_dm_session_key_is_stable():
    src = _dm()
    assert build_session_key(src) == "agent:main:wecom:dm:default:corp1:userA"


def test_dm_session_key_falls_back_to_user_when_no_chat():
    src = SessionSource(platform="wecom", chat_id="", chat_type="dm", user_id="u1")
    assert build_session_key(src) == "agent:main:wecom:dm:u1"


def test_group_session_key_isolates_per_user_by_default():
    src = SessionSource(
        platform="wecom", chat_id="roomA", chat_type="group", user_id="u1"
    )
    assert build_session_key(src) == "agent:main:wecom:group:roomA:u1"


def test_group_session_key_can_be_shared():
    src = SessionSource(
        platform="wecom", chat_id="roomA", chat_type="group", user_id="u1"
    )
    assert build_session_key(src, group_sessions_per_user=False) == "agent:main:wecom:group:roomA"


def test_thread_session_key_is_shared_by_default():
    src = SessionSource(
        platform="telegram", chat_id="chatX", chat_type="thread", thread_id="t1", user_id="u1"
    )
    assert build_session_key(src) == "agent:main:telegram:thread:chatX:t1"


def test_thread_session_key_can_isolate_per_user():
    src = SessionSource(
        platform="telegram", chat_id="chatX", chat_type="thread", thread_id="t1", user_id="u1"
    )
    assert build_session_key(src, thread_sessions_per_user=True) == (
        "agent:main:telegram:thread:chatX:t1:u1"
    )


# ---------------------------------------------------------------------------
# split_message / delivery
# ---------------------------------------------------------------------------


def test_split_short_message_returns_single_chunk():
    assert split_message("hello", max_length=100) == ["hello"]


def test_split_breaks_on_blank_lines_first():
    content = "para one\n\npara two\n\npara three"
    chunks = split_message(content, max_length=12)
    assert all(len(c) <= 12 for c in chunks)
    # Reconstructible content (whitespace-trimmed at splits).
    assert " ".join(chunks).split() == content.split()


def test_split_falls_back_to_hard_cut_when_no_separator():
    chunks = split_message("abcdefghij" * 10, max_length=15)
    assert all(len(c) <= 15 for c in chunks)
    assert "".join(chunks) == ("abcdefghij" * 10)


def test_deliver_final_response_chunks_and_records_continuation():
    sent: list[tuple[str, dict]] = []

    class FakeAdapter(BasePlatformAdapter):
        platform_name = "fake"
        capabilities = PlatformCapabilities(max_message_length=10, final_only=True)

        async def connect(self):  # noqa: D401
            return True

        async def disconnect(self):
            return None

        async def send(self, chat_id, content, *, reply_to=None, metadata=None):
            sent.append((content, dict(metadata or {})))
            return SendResult(success=True, message_id=f"mid-{len(sent)}")

    src = _dm()
    result = asyncio.run(
        deliver_final_response(
            adapter=FakeAdapter(), source=src, content="a" * 25, reply_to="r-1"
        )
    )

    assert result.success
    assert result.message_id == "mid-3"
    assert result.continuation_message_ids == ("mid-1", "mid-2")
    assert len(sent) == 3
    # Metadata carried through so adapter.send can resolve account/user.
    for _, meta in sent:
        assert meta["account_id"] == "default"
        assert meta["user_id"] == "userA"


def test_deliver_final_response_short_circuits_on_failure():
    class FakeAdapter(BasePlatformAdapter):
        platform_name = "fake"
        capabilities = PlatformCapabilities(max_message_length=5, final_only=True)

        async def connect(self):
            return True

        async def disconnect(self):
            return None

        async def send(self, chat_id, content, *, reply_to=None, metadata=None):
            return SendResult(success=False, error="boom")

    src = _dm()
    result = asyncio.run(
        deliver_final_response(
            adapter=FakeAdapter(), source=src, content="abcdefghij", reply_to=None
        )
    )
    assert not result.success
    assert result.error == "boom"


# ---------------------------------------------------------------------------
# turn queue
# ---------------------------------------------------------------------------


def test_turn_queue_serializes_same_session():
    q = SessionTurnQueue()
    order: list[str] = []

    async def step(label: str):
        order.append(f"start-{label}")
        await asyncio.sleep(0.01)
        order.append(f"end-{label}")

    async def scenario():
        await asyncio.gather(q.run("s1", lambda: step("a")), q.run("s1", lambda: step("b")))

    asyncio.run(scenario())
    # b must start after a ends (no interleaving).
    assert order.index("end-a") < order.index("start-b")


def test_turn_queue_runs_different_sessions_in_parallel():
    q = SessionTurnQueue()
    started: list[str] = []

    async def step(label: str):
        started.append(label)
        await asyncio.sleep(0.02)

    async def scenario():
        await asyncio.gather(
            q.run("s1", lambda: step("s1")),
            q.run("s2", lambda: step("s2")),
        )

    asyncio.run(scenario())
    # Both should have started within the same window.
    assert set(started) == {"s1", "s2"}


# ---------------------------------------------------------------------------
# TtlSet / dedup / rate limit
# ---------------------------------------------------------------------------


def test_ttl_set_seen_or_mark_returns_false_then_true():
    s = TtlSet(ttl_seconds=60)
    assert s.seen_or_mark("k1") is False
    assert s.seen_or_mark("k1") is True
    assert s.seen_or_mark("k2") is False


def test_ttl_set_empty_key_is_noop():
    s = TtlSet(ttl_seconds=60)
    assert s.seen_or_mark("") is False
    assert s.seen_or_mark("") is False


def test_dedup_content_key_is_stable():
    k1 = MessageDeduplicator.content_key("acc", "u1", "hi")
    k2 = MessageDeduplicator.content_key("acc", "u1", "hi")
    k3 = MessageDeduplicator.content_key("acc", "u1", "bye")
    assert k1 == k2
    assert k1 != k3


def test_rate_limit_circuit_opens_after_threshold():
    c = RateLimitCircuit(threshold=1, window_seconds=30, open_seconds=30)
    assert not c.is_open()
    c.record()
    assert c.is_open()


# ---------------------------------------------------------------------------
# iLink protocol normalization
# ---------------------------------------------------------------------------


def test_get_updates_buf_prefers_specific_key():
    assert get_updates_buf({"get_updates_buf": "v1", "sync_buf": "v2"}) == "v1"


def test_get_updates_buf_falls_back_to_sync_buf():
    assert get_updates_buf({"sync_buf": "v2"}) == "v2"


def test_extract_messages_supports_both_field_names():
    assert extract_messages({"msgs": [{"a": 1}]}) == [{"a": 1}]
    assert extract_messages({"messages": [{"b": 2}]}) == [{"b": 2}]
    assert extract_messages({}) == []


def test_extract_text_from_text_item():
    items = [
        {"text_item": {"content": "hello"}},
        {"text_item": {"text": "world"}},
        {"content": "！"},
    ]
    assert extract_text(items) == "hello\nworld\n！"


def test_guess_chat_type_returns_dm_for_direct_message():
    raw = {"from_user_id": "u1"}
    assert guess_chat_type(raw, "acc") == ("dm", "u1")


def test_guess_chat_type_returns_group_for_chat_room():
    raw = {"from_user_id": "u1", "chat_room_id": "roomX"}
    assert guess_chat_type(raw, "acc") == ("group", "roomX")


def test_normalize_message_skips_self_message():
    assert normalize_message({"from_user_id": "acc"}, "acc") is None


def test_normalize_message_builds_dm_payload():
    raw = {
        "from_user_id": "u1",
        "message_id": "mid-1",
        "item_list": [{"text_item": {"content": "hi"}}],
        "context_token": "ctx-abc",
    }
    msg = normalize_message(raw, "acc")
    assert msg is not None
    assert msg.from_user_id == "u1"
    assert msg.chat_type == "dm"
    assert msg.text == "hi"
    assert msg.context_token == "ctx-abc"


def test_check_errcode_raises_rate_limited():
    with pytest.raises(IlinkRateLimited):
        check_errcode({"errcode": -2})


def test_check_errcode_raises_session_expired():
    with pytest.raises(IlinkSessionExpired):
        check_errcode({"errcode": -14})


def test_check_errcode_passthrough_on_ok():
    check_errcode({"errcode": 0})
    check_errcode({})


def test_check_errcode_raises_on_generic_ilink_failure():
    from src.gateway.platforms import ilink_protocol

    IlinkProtocolError = getattr(ilink_protocol, "IlinkProtocolError")
    with pytest.raises(IlinkProtocolError) as exc:
        check_errcode({"ret": -1, "errcode": 123, "errmsg": "bad auth"})
    assert "bad auth" in str(exc.value)


def test_hash_token_is_stable_and_short():
    h = hash_token("bot_xxx")
    assert h and len(h) == 16
    assert hash_token("bot_xxx") == h
    assert hash_token("bot_yyy") != h
    assert hash_token("") == ""


# ---------------------------------------------------------------------------
# platform locks
# ---------------------------------------------------------------------------


def test_lock_identity_for_token_is_hashed():
    from src.gateway.locks import lock_identity_for_adapter

    scope, identity = lock_identity_for_adapter(platform="weixin", bot_token="bot_secret")
    assert scope == "weixin-bot-token"
    assert identity.startswith("sha256:")
    assert "bot_secret" not in identity


def test_weixin_lock_uses_scan_login_credential_file(tmp_path: Path):
    from src.gateway.adapters import _build_weixin
    from src.gateway.router import atomic_write_json

    atomic_write_json(
        tmp_path / "weixin_credentials.json",
        {"account_id": "acc-from-file", "bot_token": "secret-from-file"},
    )

    build = _build_weixin({"account_id": "", "bot_token": ""}, data_dir=tmp_path)

    assert build is not None
    assert build.lock_scope == "weixin-bot-token"
    assert build.lock_identity.startswith("sha256:")
    assert "secret-from-file" not in build.lock_identity
    assert build.account_label == "acc-from-file"


def test_doctor_lock_check_uses_scan_login_credential_file(tmp_path: Path):
    from src.gateway.doctor import DoctorReport, _check_platform_locks
    from src.gateway.locks import PlatformLockManager, lock_identity_for_adapter
    from src.gateway.router import atomic_write_json

    atomic_write_json(
        tmp_path / "weixin_credentials.json",
        {"account_id": "acc-from-file", "bot_token": "secret-from-file"},
    )
    lock_manager = PlatformLockManager(locks_dir=tmp_path / "locks", owner="owner-a")
    scope, identity = lock_identity_for_adapter(platform="weixin", bot_token="secret-from-file")
    assert lock_manager.acquire(scope=scope, identity=identity, platform="weixin").acquired

    report = DoctorReport()
    _check_platform_locks(
        report,
        {"platforms": {"weixin": {"enabled": True, "account_id": "", "bot_token": ""}}},
        lock_manager,
        data_dir=tmp_path,
    )

    check = next(c for c in report.checks if c.name == "lock_weixin")
    assert not check.ok
    assert check.severity == "fatal"
    assert "owner-a" in check.message


def test_lock_identity_for_app_is_unhashed():
    from src.gateway.locks import lock_identity_for_adapter

    scope, identity = lock_identity_for_adapter(
        platform="wecom", corp_id="corp123", agent_id="1000002"
    )
    assert scope == "wecom-app"
    assert identity == "corp123:1000002"


def test_lock_acquire_then_release(tmp_path: Path):
    from src.gateway.locks import PlatformLockManager

    mgr = PlatformLockManager(locks_dir=tmp_path / "locks", owner="test")
    r1 = mgr.acquire(
        scope="wecom-app",
        identity="corp1:1000001",
        platform="wecom",
        config_path=str(tmp_path / "gateway.yaml"),
    )
    assert r1.acquired

    # A second manager (different pid) cannot acquire the same identity.
    mgr2 = PlatformLockManager(locks_dir=tmp_path / "locks", owner="test2")
    r2 = mgr2.acquire(scope="wecom-app", identity="corp1:1000001", platform="wecom")
    assert not r2.acquired
    assert r2.owner_info is not None

    # After release, the second manager can acquire.
    assert mgr.release(scope="wecom-app", identity="corp1:1000001")
    r3 = mgr2.acquire(scope="wecom-app", identity="corp1:1000001", platform="wecom")
    assert r3.acquired


def test_wecom_lock_targets_include_every_configured_app():
    from src.gateway.adapters import _wecom_lock_targets

    targets = _wecom_lock_targets(
        [
            {"name": "ops", "corp_id": "corp1", "agent_id": "1001"},
            {"name": "sales", "corp_id": "corp2", "agent_id": "1002"},
        ]
    )

    assert [(t.scope, t.identity, t.account_label) for t in targets] == [
        ("wecom-app", "corp1:1001", "ops"),
        ("wecom-app", "corp2:1002", "sales"),
    ]


def test_cli_accepts_config_after_subcommand():
    import gateway

    args = gateway.build_parser().parse_args(["doctor", "--config", "gateway.yaml"])
    assert args.command == "doctor"
    assert args.config == Path("gateway.yaml")


def test_wecom_verify_url_returns_plain_text_response():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.gateway.platforms.wecom_webhook import WecomWebhookAdapter

    class FakeCryptor:
        def verify_url(self, **kwargs):
            return "plain-echostr"

    app = FastAPI()
    adapter = WecomWebhookAdapter({"apps": []}, app)
    adapter._apps = [SimpleNamespace(cryptor=FakeCryptor())]  # noqa: SLF001
    adapter._register_routes()  # noqa: SLF001

    response = TestClient(app).get(
        "/wecom/callback",
        params={
            "msg_signature": "sig",
            "timestamp": "1",
            "nonce": "n",
            "echostr": "encrypted",
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.content == b"plain-echostr"


def test_weixin_group_send_uses_group_peer_and_context_token(tmp_path: Path):
    from src.gateway.platforms.weixin_ilink import IlinkCredentials, WeixinIlinkAdapter

    async def scenario():
        adapter = WeixinIlinkAdapter({"send_chunk_delay_seconds": 0}, data_dir=tmp_path)
        adapter._creds = IlinkCredentials(  # noqa: SLF001
            account_id="acc",
            bot_token="token",
            base_url="https://example.invalid",
            cdn_base_url="",
            bot_type="3",
            extras={},
        )
        adapter._token_store.set("acc", "room-1", "ctx-room")  # noqa: SLF001
        sent: list[tuple[str, str]] = []

        async def fake_send_one_text(*, peer_id: str, text: str, context_token: str):
            sent.append((peer_id, context_token))
            return {"errcode": 0, "message_id": "m1"}

        adapter._send_one_text = fake_send_one_text  # type: ignore[method-assign] # noqa: SLF001
        result = await adapter.send(
            "room-1",
            "hello group",
            metadata={"account_id": "acc", "user_id": "u1", "chat_type": "group"},
        )
        assert result.success
        assert sent == [("room-1", "ctx-room")]

    asyncio.run(scenario())


def test_runner_start_writes_runtime_status(tmp_path: Path, monkeypatch):
    from src.gateway.adapters import AdapterBuild
    from src.gateway.base import BasePlatformAdapter, SendResult
    from src.gateway.runner import GatewayRunner
    from src.gateway.status import read_status

    class FakeAdapter(BasePlatformAdapter):
        platform_name = "fake"

        async def connect(self):
            return True

        async def disconnect(self):
            return None

        async def send(self, chat_id, content, *, reply_to=None, metadata=None):
            return SendResult(success=True)

    def fake_build_adapters(**kwargs):
        return [
            AdapterBuild(
                adapter=FakeAdapter(),
                platform="fake",
                account_label="default",
                lock_scope="",
                lock_identity="",
            )
        ]

    monkeypatch.setattr("src.gateway.runner.build_adapters", fake_build_adapters)
    status_file = tmp_path / "status.json"
    config = {
        "data_dir": str(tmp_path / "data"),
        "server": {"host": "127.0.0.1", "port": 8645},
        "logging": {"file": str(tmp_path / "gateway.log")},
        "service": {"name": "mini-agent-gateway", "status_file": str(status_file)},
        "platforms": {"weixin": {"enabled": True}},
    }

    async def scenario():
        runner = GatewayRunner(config, config_path=tmp_path / "gateway.yaml")
        await runner.start()
        try:
            status = read_status(status_file)
        finally:
            await runner.stop()
        return status

    status = asyncio.run(scenario())
    assert status is not None
    assert status["state"] == "running"
    assert status["enabled_platforms"] == ["fake"]
    assert status["host"] == "127.0.0.1"


# ---------------------------------------------------------------------------
# init: env -> gateway.yaml generation
# ---------------------------------------------------------------------------


def test_init_no_creds_disables_all_platforms():
    from src.gateway.init import build_config_from_env, enabled_platforms

    config = build_config_from_env({})
    assert enabled_platforms(config) == []
    assert config["platforms"]["wecom"]["enabled"] is False
    assert config["platforms"]["wecom"]["apps"] == []
    assert config["platforms"]["weixin"]["enabled"] is False


def test_init_full_wecom_creds_auto_enables_with_one_app():
    from src.gateway.init import build_config_from_env

    config = build_config_from_env(
        {
            "WECOM_CORP_ID": "corp1",
            "WECOM_AGENT_ID": "1000002",
            "WECOM_SECRET": "secret",
            "WECOM_TOKEN": "tok",
            "WECOM_AES_KEY": "k" * 43,
        }
    )
    wecom = config["platforms"]["wecom"]
    assert wecom["enabled"] is True
    assert len(wecom["apps"]) == 1
    assert wecom["apps"][0]["corp_id"] == "corp1"
    assert wecom["apps"][0]["agent_id"] == "1000002"


def test_init_partial_wecom_creds_stays_disabled():
    from src.gateway.init import build_config_from_env

    # Missing WECOM_AES_KEY -> not all required present -> disabled.
    config = build_config_from_env(
        {
            "WECOM_CORP_ID": "corp1",
            "WECOM_AGENT_ID": "1000002",
            "WECOM_SECRET": "secret",
            "WECOM_TOKEN": "tok",
        }
    )
    assert config["platforms"]["wecom"]["enabled"] is False
    assert config["platforms"]["wecom"]["apps"] == []


def test_init_weixin_creds_auto_enable_and_parse_allowlist():
    from src.gateway.init import build_config_from_env

    config = build_config_from_env(
        {
            "WEIXIN_ACCOUNT_ID": "acc",
            "WEIXIN_TOKEN": "tok",
            "WEIXIN_ALLOW_FROM": "u1, u2 ,, u3",
        }
    )
    weixin = config["platforms"]["weixin"]
    assert weixin["enabled"] is True
    assert weixin["account_id"] == "acc"
    # Whitespace trimmed, empties dropped.
    assert weixin["allow_from"] == ["u1", "u2", "u3"]


def test_init_applies_defaults_and_overrides():
    from src.gateway.init import build_config_from_env

    default_cfg = build_config_from_env({})
    assert default_cfg["server"]["host"] == "0.0.0.0"
    assert default_cfg["server"]["port"] == 8645
    assert default_cfg["data_dir"] == "~/.mini-agent/gateway"
    assert default_cfg["platforms"]["weixin"]["dm_policy"] == "allowlist"

    override = build_config_from_env(
        {"GATEWAY_HOST": "127.0.0.1", "GATEWAY_PORT": "9001", "GATEWAY_DATA_DIR": "/tmp/gw"}
    )
    assert override["server"]["host"] == "127.0.0.1"
    assert override["server"]["port"] == 9001
    assert override["data_dir"] == "/tmp/gw"
    # Sub-paths derive from data_dir.
    assert override["locks"]["dir"] == "/tmp/gw/locks"


def test_init_render_yaml_round_trips_through_load_config(tmp_path: Path):
    from src.gateway.config import load_config
    from src.gateway.init import build_config_from_env, render_yaml

    config = build_config_from_env(
        {
            "WEIXIN_ACCOUNT_ID": "acc",
            "WEIXIN_TOKEN": "tok",
            "WEIXIN_ALLOW_FROM": "alice,bob",
            "GATEWAY_PORT": "8700",
        }
    )
    out = tmp_path / "gateway.yaml"
    out.write_text(render_yaml(config), encoding="utf-8")

    loaded = load_config(out)
    assert loaded["platforms"]["weixin"]["enabled"] is True
    assert loaded["platforms"]["weixin"]["allow_from"] == ["alice", "bob"]
    assert loaded["server"]["port"] == 8700
    assert loaded["platforms"]["wecom"]["enabled"] is False


# ---------------------------------------------------------------------------
# weixin iLink QR login state machine (_run_qr_login)
#
# The HTTP transport is injected, so we drive the full wait -> scaned ->
# confirmed flow with a scripted ``get`` and fake sleep/clock — no network,
# no real time, mirroring the file's "transport is injectable" design.
# ---------------------------------------------------------------------------


def _scripted_get(responses):
    """Return an async ``get(base_url, endpoint)`` popping ``responses`` in order.

    Also records each ``(base_url, endpoint)`` call into the returned ``.calls``
    list so tests can assert which host a poll targeted.
    """
    calls: list = []

    async def _get(base_url, endpoint):
        calls.append((base_url, endpoint))
        return responses.pop(0)

    _get.calls = calls
    return _get


async def _no_sleep(_seconds):
    return None


def test_qr_login_returns_credentials_on_confirm():
    from src.gateway.platforms.weixin_ilink import IlinkCredentials, _run_qr_login

    get = _scripted_get(
        [
            {"qrcode": "HEX123", "qrcode_img_content": "https://scan/url"},
            {"status": "wait"},
            {"status": "scaned"},
            {
                "status": "confirmed",
                "ilink_bot_id": "bot-1",
                "bot_token": "tok-9",
                "baseurl": "https://shard.weixin.qq.com",
                "ilink_user_id": "user-7",
            },
        ]
    )
    creds = asyncio.run(
        _run_qr_login(get=get, bot_type="3", render_qr=lambda v, u: None, sleep=_no_sleep, now=lambda: 0.0)
    )
    assert isinstance(creds, IlinkCredentials)
    assert creds.account_id == "bot-1"
    assert creds.bot_token == "tok-9"
    assert creds.base_url == "https://shard.weixin.qq.com"
    assert creds.extras["user_id"] == "user-7"
    # First GET fetches the QR for the requested bot_type.
    assert "get_bot_qrcode?bot_type=3" in get.calls[0][1]


def test_qr_login_returns_none_on_timeout():
    from src.gateway.platforms.weixin_ilink import _run_qr_login

    async def _always_wait(base_url, endpoint):
        if "get_bot_qrcode" in endpoint:
            return {"qrcode": "HEX", "qrcode_img_content": "https://u"}
        return {"status": "wait"}

    # Stepping clock crosses the (tiny) deadline after a few polls.
    ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0])
    creds = asyncio.run(
        _run_qr_login(
            get=_always_wait,
            bot_type="3",
            render_qr=lambda v, u: None,
            sleep=_no_sleep,
            now=lambda: next(ticks),
            deadline_seconds=3,
        )
    )
    assert creds is None


def test_qr_login_follows_redirect_host_for_next_poll():
    from src.gateway.platforms.weixin_ilink import _run_qr_login

    get = _scripted_get(
        [
            {"qrcode": "HEX", "qrcode_img_content": "https://u"},
            {"status": "scaned_but_redirect", "redirect_host": "shard9.weixin.qq.com"},
            {"status": "confirmed", "ilink_bot_id": "b", "bot_token": "t"},
        ]
    )
    creds = asyncio.run(
        _run_qr_login(get=get, bot_type="3", render_qr=lambda v, u: None, sleep=_no_sleep, now=lambda: 0.0)
    )
    assert creds is not None
    # The poll after the redirect must target the new sharded host.
    assert get.calls[2][0] == "https://shard9.weixin.qq.com"


def test_qr_login_returns_none_when_qrcode_missing():
    from src.gateway.platforms.weixin_ilink import _run_qr_login

    get = _scripted_get([{"qrcode": "", "qrcode_img_content": ""}])
    creds = asyncio.run(
        _run_qr_login(get=get, bot_type="3", render_qr=lambda v, u: None, sleep=_no_sleep, now=lambda: 0.0)
    )
    assert creds is None
    # No status polling happens when the QR fetch yields no token.
    assert len(get.calls) == 1


def test_weixin_getupdates_uses_hermes_ilink_transport_headers():
    from src.gateway.platforms.weixin_ilink import IlinkCredentials, WeixinIlinkAdapter

    class FakeResponse:
        ok = True
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def text(self):
            return '{"ret":0,"msgs":[],"get_updates_buf":"next"}'

    class FakeSession:
        def __init__(self):
            self.calls = []

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return FakeResponse()

    async def scenario():
        adapter = WeixinIlinkAdapter({}, data_dir=Path(os.devnull).parent)
        adapter._creds = IlinkCredentials(  # noqa: SLF001
            account_id="acc",
            bot_token="tok",
            base_url="https://example.invalid",
            cdn_base_url="",
            bot_type="3",
            extras={},
        )
        session = FakeSession()
        adapter._poll_session = session  # type: ignore[assignment] # noqa: SLF001
        data = await adapter._api_post("ilink/bot/getupdates", {"get_updates_buf": "cur"})  # noqa: SLF001
        return data, session.calls[0]

    data, (url, kwargs) = asyncio.run(scenario())
    assert data["get_updates_buf"] == "next"
    assert url == "https://example.invalid/ilink/bot/getupdates"
    assert "json" not in kwargs
    assert kwargs["data"] == '{"get_updates_buf":"cur","base_info":{"channel_version":"2.2.0"}}'
    headers = kwargs["headers"]
    assert headers["AuthorizationType"] == "ilink_bot_token"
    assert headers["Authorization"] == "Bearer tok"
    assert headers["iLink-App-Id"] == "bot"
    assert headers["iLink-App-ClientVersion"] == str((2 << 16) | (2 << 8) | 0)
    assert headers["Content-Length"] == str(len(kwargs["data"].encode("utf-8")))
    assert headers["X-WECHAT-UIN"] != "acc"


def test_weixin_send_text_uses_hermes_message_shape(tmp_path: Path):
    from src.gateway.platforms.weixin_ilink import IlinkCredentials, WeixinIlinkAdapter

    class FakeResponse:
        ok = True
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def text(self):
            return '{"ret":0,"message_id":"m1"}'

    class FakeSession:
        def __init__(self):
            self.calls = []

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return FakeResponse()

    async def scenario():
        adapter = WeixinIlinkAdapter({"send_chunk_delay_seconds": 0}, data_dir=tmp_path)
        adapter._creds = IlinkCredentials(  # noqa: SLF001
            account_id="acc",
            bot_token="tok",
            base_url="https://example.invalid",
            cdn_base_url="",
            bot_type="3",
            extras={},
        )
        adapter._send_session = FakeSession()  # type: ignore[assignment] # noqa: SLF001
        data = await adapter._send_one_text(  # noqa: SLF001
            peer_id="u1",
            text="hello",
            context_token="ctx",
        )
        return data, adapter._send_session.calls[0]  # type: ignore[attr-defined] # noqa: SLF001

    data, (_url, kwargs) = asyncio.run(scenario())
    assert data["message_id"] == "m1"
    payload = __import__("json").loads(kwargs["data"])
    assert payload["base_info"] == {"channel_version": "2.2.0"}
    assert set(payload) == {"msg", "base_info"}
    msg = payload["msg"]
    assert msg["from_user_id"] == ""
    assert msg["to_user_id"] == "u1"
    assert msg["message_type"] == 2
    assert msg["message_state"] == 2
    assert msg["context_token"] == "ctx"
    assert msg["item_list"] == [{"type": 1, "text_item": {"text": "hello"}}]
    assert msg["client_id"].startswith("mini-agent-weixin-")
