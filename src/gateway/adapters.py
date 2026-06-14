"""Adapter factory: build enabled adapters from config.

Keeps the runner free of platform-specific branching. New platforms plug in
here by adding one branch; the rest of the gateway stays generic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from src.gateway.base import BasePlatformAdapter
from src.gateway.locks import PlatformLockManager, lock_identity_for_adapter
from src.gateway.router import load_json

logger = logging.getLogger(__name__)


@dataclass
class LockTarget:
    scope: str
    identity: str
    account_label: str


@dataclass
class AdapterBuild:
    adapter: BasePlatformAdapter
    platform: str
    account_label: str
    lock_scope: str
    lock_identity: str
    lock_targets: list[LockTarget] = field(default_factory=list)


def _wecom_lock_targets(apps_cfg: list[Any]) -> list[LockTarget]:
    targets: list[LockTarget] = []
    for raw in apps_cfg:
        corp_id = getattr(raw, "corp_id", None)
        agent_id = getattr(raw, "agent_id", None)
        name = getattr(raw, "name", None)
        if isinstance(raw, dict):
            corp_id = raw.get("corp_id")
            agent_id = raw.get("agent_id")
            name = raw.get("name")
        corp_id = str(corp_id or "")
        agent_id = str(agent_id or "")
        if not corp_id or not agent_id:
            continue
        scope, identity = lock_identity_for_adapter(
            platform="wecom",
            corp_id=corp_id,
            agent_id=agent_id,
        )
        targets.append(LockTarget(scope=scope, identity=identity, account_label=str(name or "default")))
    return targets


def _build_wecom(config: dict, app: Any) -> Optional[AdapterBuild]:
    from src.gateway.platforms.wecom_webhook import WecomWebhookAdapter

    adapter = WecomWebhookAdapter(config, app)
    apps = adapter._apps  # noqa: SLF001 - introspect for lock identity
    primary = apps[0] if apps else None
    lock_targets = _wecom_lock_targets(apps)
    first_target = lock_targets[0] if lock_targets else None
    scope, identity = (
        (first_target.scope, first_target.identity) if first_target is not None else ("", "")
    )
    account_label = primary.name if primary else "default"
    return AdapterBuild(
        adapter=adapter,
        platform="wecom",
        account_label=account_label,
        lock_scope=scope,
        lock_identity=identity,
        lock_targets=lock_targets,
    )


def _weixin_credentials_for_lock(config: dict, data_dir: Optional[Path]) -> tuple[str, str]:
    account_id = str(config.get("account_id") or "")
    bot_token = str(config.get("bot_token") or "")
    if bot_token:
        return account_id, bot_token

    creds_dir = data_dir or Path("~/.mini-agent/gateway").expanduser()
    data = load_json(creds_dir / "weixin_credentials.json", default=None)
    if not isinstance(data, dict):
        return account_id, bot_token
    return (
        str(data.get("account_id") or account_id),
        str(data.get("bot_token") or bot_token),
    )


def _build_weixin(config: dict, data_dir: Optional[Path] = None) -> Optional[AdapterBuild]:
    from src.gateway.platforms.weixin_ilink import WeixinIlinkAdapter

    adapter = WeixinIlinkAdapter(config, data_dir=data_dir)
    account_id, bot_token = _weixin_credentials_for_lock(config, data_dir)
    scope, identity = lock_identity_for_adapter(platform="weixin", bot_token=bot_token)
    account_label = account_id or "default"
    return AdapterBuild(
        adapter=adapter,
        platform="weixin",
        account_label=account_label,
        lock_scope=scope,
        lock_identity=identity,
    )


def build_adapters(
    *,
    config: dict,
    app: Any,
    lock_manager: PlatformLockManager,
    config_path: str = "",
    command: str = "",
) -> list[AdapterBuild]:
    """Construct enabled adapters and acquire their resource locks.

    Adapters whose lock is held by another process are skipped (their
    ``connect`` would just fail anyway). Locks are owned by ``lock_manager``
    so the runner can release them on shutdown.
    """

    platforms = config.get("platforms") or {}
    builds: list[AdapterBuild] = []

    if (platforms.get("wecom") or {}).get("enabled"):
        build = _build_wecom(platforms["wecom"], app)
        if build is not None and _acquire(build, lock_manager, config_path, command):
            builds.append(build)

    if (platforms.get("weixin") or {}).get("enabled"):
        data_dir = Path(config.get("data_dir") or "~/.mini-agent/gateway").expanduser()
        build = _build_weixin(platforms["weixin"], data_dir=data_dir)
        if build is not None and _acquire(build, lock_manager, config_path, command):
            builds.append(build)

    return builds


def _acquire(
    build: AdapterBuild,
    lock_manager: PlatformLockManager,
    config_path: str,
    command: str,
) -> bool:
    targets = build.lock_targets or [
        LockTarget(build.lock_scope, build.lock_identity, build.account_label)
    ]
    targets = [target for target in targets if target.identity]
    if not targets:
        # Token/app not configured; let the adapter itself mark fatal on connect.
        return True

    acquired: list[LockTarget] = []
    for target in targets:
        result = lock_manager.acquire(
            scope=target.scope,
            identity=target.identity,
            platform=build.platform,
            config_path=config_path,
            command=command,
            extra={"account_label": target.account_label},
        )
        if result.acquired:
            acquired.append(target)
            continue
        for prior in acquired:
            lock_manager.release(scope=prior.scope, identity=prior.identity)
        owner = result.owner_info
        if owner is not None:
            build.adapter.mark_fatal(
                code="lock_held",
                message=(
                    f"{build.platform} resource {target.account_label} already "
                    f"held by pid {owner.pid} ({owner.owner}) since {owner.started_at}"
                ),
            )
        else:
            build.adapter.mark_fatal("lock_held", result.error or "lock held")
        logger.warning("skip adapter %s: %s", build.platform, result.error)
        return False
    return True
