#!/usr/bin/env python3
"""
xui_limiter.py - Professional 3x-ui Client Limiter

Powered By Piriz  |  Telegram: @ThePiriz

Two enforcement modes run side-by-side:

1. IP limit  - tails xray's access.log, counts unique source IPs per client
               within a sliding window. If a client exceeds the configured
               threshold of concurrent IPs, the client is disabled, renamed
               with `-iplimited`, and its credential (UUID / password) is
               rotated so the leaked config stops working.

2. Traffic limit - polls the 3x-ui client_traffics table on a short interval,
                   keeps a rolling buffer of (up+down) snapshots, and compares
                   the current snapshot with the snapshot from N minutes ago.
                   If the delta exceeds the threshold, the client is disabled,
                   renamed with `-trafficlimited`, and credential rotated.

After applying limits, x-ui is restarted (debounced) so xray reloads its
in-memory config and drops the abuser. Clients are never deleted from the
database - they are renamed and disabled, preserving traffic counters and
any external references.

Each limit action emits an event. Events are POSTed to a webhook URL and
also kept in a local in-memory ring buffer for inspection via HTTP API.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import logging.handlers
import os
import random
import re
import signal
import sqlite3
import string
import sys
import time
import uuid
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml
from aiohttp import ClientSession, ClientTimeout, web

log = logging.getLogger("xui_limiter")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class IpLimitConfig:
    enabled: bool = True
    max_ips: int = 5
    window_seconds: int = 60
    sustain_seconds: int = 20
    suffix: str = "-iplimited"
    access_log: str = "/usr/local/x-ui/bin/access.log"


@dataclass
class TrafficLimitConfig:
    enabled: bool = True
    snapshot_interval_seconds: int = 10
    comparison_window_seconds: int = 300
    threshold_bytes: int = 5 * 1024 * 1024 * 1024  # 5 GB
    suffix: str = "-trafficlimited"


@dataclass
class WebhookConfig:
    enabled: bool = False
    url: str = ""
    timeout_seconds: int = 5
    retries: int = 3
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class TelegramConfig:
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""  # supports user, group, supergroup, channel ids
    # message_thread_id for forum topics inside supergroups; leave empty otherwise
    message_thread_id: str = ""
    timeout_seconds: int = 10
    retries: int = 3
    parse_mode: str = "HTML"


@dataclass
class ApiConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 9999
    token: str = "change-me"
    retention_events: int = 500


@dataclass
class RestartConfig:
    debounce_seconds: int = 5
    service: str = "x-ui"
    command: list[str] = field(default_factory=lambda: ["systemctl", "restart", "x-ui"])


@dataclass
class WhitelistConfig:
    name_prefixes: list[str] = field(default_factory=lambda: ["vip-"])
    emails: list[str] = field(default_factory=list)
    suffix_blocklist: list[str] = field(
        default_factory=lambda: ["-iplimited", "-trafficlimited"]
    )


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "/var/log/xui-limiter.log"
    max_size_mb: int = 50
    backup_count: int = 3


@dataclass
class Config:
    database_path: str = "/etc/x-ui/x-ui.db"
    ip_limit: IpLimitConfig = field(default_factory=IpLimitConfig)
    traffic_limit: TrafficLimitConfig = field(default_factory=TrafficLimitConfig)
    webhook: WebhookConfig = field(default_factory=WebhookConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    restart: RestartConfig = field(default_factory=RestartConfig)
    whitelist: WhitelistConfig = field(default_factory=WhitelistConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return cls(
            database_path=raw.get("database_path", cls.database_path),
            ip_limit=IpLimitConfig(**raw.get("ip_limit", {})),
            traffic_limit=TrafficLimitConfig(**raw.get("traffic_limit", {})),
            webhook=WebhookConfig(**raw.get("webhook", {})),
            telegram=TelegramConfig(**raw.get("telegram", {})),
            api=ApiConfig(**raw.get("api", {})),
            restart=RestartConfig(**raw.get("restart", {})),
            whitelist=WhitelistConfig(**raw.get("whitelist", {})),
            logging=LoggingConfig(**raw.get("logging", {})),
        )


# ---------------------------------------------------------------------------
# Database layer
# ---------------------------------------------------------------------------


@dataclass
class ClientRecord:
    inbound_id: int
    protocol: str
    email: str
    credential_field: str  # "id" or "password"
    credential_value: str
    enable: bool
    comment: str
    inbound_settings: dict
    client_index: int  # index in inbound_settings["clients"]


class Database:
    """Thin wrapper around 3x-ui's SQLite database.

    All writes go through `with self._writer() as cur` so we serialize from
    our side and rely on SQLite's busy-timeout for cross-process contention
    with x-ui itself.
    """

    PROTOCOL_CRED_FIELD = {
        "vless": "id",
        "vmess": "id",
        "trojan": "password",
        "shadowsocks": "password",
    }

    def __init__(self, path: str, timeout: float = 30.0) -> None:
        self.path = path
        self.timeout = timeout
        if not Path(path).exists():
            raise FileNotFoundError(f"x-ui database not found at {path}")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @contextmanager
    def _writer(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()
            try:
                yield cur
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    def list_client_traffics(self) -> dict[str, tuple[int, int]]:
        """Return {email: (up+down, enable)} for all clients in client_traffics."""
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT email, up, down, enable FROM client_traffics"
            )
            result: dict[str, tuple[int, int]] = {}
            for row in cur:
                result[row["email"]] = (
                    int(row["up"] or 0) + int(row["down"] or 0),
                    int(row["enable"] or 0),
                )
            return result
        finally:
            conn.close()

    def find_client(self, email: str) -> Optional[ClientRecord]:
        """Locate a client by email across all inbounds."""
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT id, protocol, settings FROM inbounds WHERE enable = 1"
            )
            for row in cur:
                try:
                    settings = json.loads(row["settings"])
                except (TypeError, json.JSONDecodeError):
                    continue
                clients = settings.get("clients") or []
                for idx, client in enumerate(clients):
                    if client.get("email") == email:
                        protocol = (row["protocol"] or "").lower()
                        cred_field = self.PROTOCOL_CRED_FIELD.get(protocol, "id")
                        return ClientRecord(
                            inbound_id=row["id"],
                            protocol=protocol,
                            email=email,
                            credential_field=cred_field,
                            credential_value=str(client.get(cred_field, "")),
                            enable=bool(client.get("enable", True)),
                            comment=str(client.get("comment", "")),
                            inbound_settings=settings,
                            client_index=idx,
                        )
            return None
        finally:
            conn.close()

    def email_exists(self, email: str) -> bool:
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT 1 FROM client_traffics WHERE email = ? LIMIT 1",
                (email,),
            )
            return cur.fetchone() is not None
        finally:
            conn.close()

    def apply_limit(
        self,
        record: ClientRecord,
        new_email: str,
        new_credential: str,
        comment_note: str,
    ) -> None:
        """Atomically rename + rotate credential + disable a client.

        Updates both `inbounds.settings` (the JSON blob xray reads from) and
        `client_traffics` (the per-email enable/quota row).
        """
        settings = record.inbound_settings
        client = settings["clients"][record.client_index]
        client["email"] = new_email
        client[record.credential_field] = new_credential
        client["enable"] = False
        existing_comment = str(client.get("comment", "")).strip()
        merged = (existing_comment + " | " + comment_note).strip(" |")
        client["comment"] = merged

        new_settings_json = json.dumps(settings, ensure_ascii=False)

        with self._writer() as cur:
            cur.execute(
                "UPDATE inbounds SET settings = ? WHERE id = ?",
                (new_settings_json, record.inbound_id),
            )
            cur.execute(
                "UPDATE client_traffics SET email = ?, enable = 0 WHERE email = ?",
                (new_email, record.email),
            )


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@dataclass
class LimitEvent:
    id: str
    timestamp: str
    type: str  # "ip_limit" | "traffic_limit"
    email_original: str
    email_new: str
    inbound_id: int
    protocol: str
    credential_field: str
    credential_old_masked: str
    credential_new_masked: str
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_public_dict(self) -> dict[str, Any]:
        return asdict(self)


def _mask_credential(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


class EventStore:
    """In-memory ring buffer of recent events, served via HTTP API."""

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._events: deque[LimitEvent] = deque(maxlen=capacity)
        self._lock = asyncio.Lock()

    async def add(self, event: LimitEvent) -> None:
        async with self._lock:
            self._events.append(event)

    async def list_after(self, since_id: Optional[str]) -> list[LimitEvent]:
        async with self._lock:
            if since_id is None:
                return list(self._events)
            seen = False
            out: list[LimitEvent] = []
            for evt in self._events:
                if seen:
                    out.append(evt)
                elif evt.id == since_id:
                    seen = True
            return out if seen else list(self._events)


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


class WebhookSender:
    def __init__(self, cfg: WebhookConfig) -> None:
        self.cfg = cfg

    async def send(self, event: LimitEvent) -> None:
        if not self.cfg.enabled or not self.cfg.url:
            return
        payload = event.to_public_dict()
        timeout = ClientTimeout(total=self.cfg.timeout_seconds)
        headers = {"Content-Type": "application/json", **self.cfg.headers}

        backoff = 1.0
        for attempt in range(1, self.cfg.retries + 1):
            try:
                async with ClientSession(timeout=timeout) as session:
                    async with session.post(
                        self.cfg.url, json=payload, headers=headers
                    ) as resp:
                        if 200 <= resp.status < 300:
                            log.info(
                                "webhook delivered event=%s status=%d",
                                event.id,
                                resp.status,
                            )
                            return
                        body = await resp.text()
                        log.warning(
                            "webhook non-2xx event=%s status=%d body=%s",
                            event.id,
                            resp.status,
                            body[:200],
                        )
            except Exception as exc:
                log.warning(
                    "webhook attempt %d/%d failed for event %s: %s",
                    attempt,
                    self.cfg.retries,
                    event.id,
                    exc,
                )
            if attempt < self.cfg.retries:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10)
        log.error("webhook giving up on event %s after %d attempts", event.id, self.cfg.retries)


# ---------------------------------------------------------------------------
# Telegram sender - formats a human-friendly Persian message and posts to
# Bot API. Stops retrying on 400 (bad chat_id / blocked) since retries won't
# fix that; keeps retrying on transient errors (5xx, network).
# ---------------------------------------------------------------------------


class TelegramSender:
    BASE = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, cfg: TelegramConfig) -> None:
        self.cfg = cfg

    def is_active(self) -> bool:
        return bool(self.cfg.enabled and self.cfg.bot_token and self.cfg.chat_id)

    async def send_event(self, event: LimitEvent) -> None:
        if not self.is_active():
            return
        await self._post(self._format_event(event), tag=f"event {event.id}")

    async def send_raw(self, text: str) -> None:
        if not self.is_active():
            return
        await self._post(text, tag="raw")

    async def _post(self, text: str, tag: str) -> None:
        url = self.BASE.format(token=self.cfg.bot_token)
        payload: dict[str, Any] = {
            "chat_id": self.cfg.chat_id,
            "text": text,
            "parse_mode": self.cfg.parse_mode,
            "disable_web_page_preview": True,
        }
        if self.cfg.message_thread_id:
            payload["message_thread_id"] = int(self.cfg.message_thread_id)

        timeout = ClientTimeout(total=self.cfg.timeout_seconds)
        backoff = 1.0
        for attempt in range(1, self.cfg.retries + 1):
            try:
                async with ClientSession(timeout=timeout) as session:
                    async with session.post(url, json=payload) as resp:
                        body = await resp.text()
                        if 200 <= resp.status < 300:
                            log.info("telegram delivered %s", tag)
                            return
                        if resp.status == 400:
                            log.error(
                                "telegram 400 for %s - check chat_id / bot_token / parse_mode. body=%s",
                                tag,
                                body[:300],
                            )
                            return  # won't fix with retry
                        if resp.status == 429:
                            # Honor Telegram's retry-after if present
                            try:
                                ra = json.loads(body).get("parameters", {}).get("retry_after", 5)
                            except Exception:
                                ra = 5
                            log.warning("telegram 429 rate-limited for %s, sleeping %ss", tag, ra)
                            await asyncio.sleep(int(ra) + 1)
                            continue
                        log.warning(
                            "telegram non-2xx for %s: status=%d body=%s",
                            tag,
                            resp.status,
                            body[:200],
                        )
            except Exception as exc:
                log.warning(
                    "telegram attempt %d/%d failed (%s): %s",
                    attempt,
                    self.cfg.retries,
                    tag,
                    exc,
                )
            if attempt < self.cfg.retries:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 15)
        log.error("telegram giving up on %s after %d attempts", tag, self.cfg.retries)

    @staticmethod
    def _esc(text: str) -> str:
        # Minimal HTML escape - Telegram HTML parse_mode requires escaping
        # & < > but allows them inside <code> blocks too if escaped.
        return (
            str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    def _format_event(self, e: LimitEvent) -> str:
        esc = self._esc
        if e.type == "ip_limit":
            title = "🚷 سرویس به دلیل اشتراک‌گذاری IP لیمیت شد"
            type_fa = "محدودیت IP"
        elif e.type == "traffic_limit":
            title = "📊 سرویس به دلیل ابیوز ترافیک لیمیت شد"
            type_fa = "محدودیت ترافیک"
        else:
            title = "⚠️ سرویس لیمیت شد"
            type_fa = e.type

        details_lines: list[str] = []
        d = e.details or {}
        if e.type == "ip_limit":
            ip_count = d.get("ip_count")
            window = d.get("window_seconds")
            sustained = d.get("sustained_seconds")
            if ip_count is not None:
                details_lines.append(
                    f"• تعداد IP منحصربه‌فرد: <b>{esc(ip_count)}</b>"
                )
            if window is not None:
                details_lines.append(f"• پنجره: {esc(window)} ثانیه")
            if sustained is not None:
                details_lines.append(f"• مدت پایداری: {esc(sustained)} ثانیه")
            sample = d.get("ips_sample") or []
            if sample:
                shown = ", ".join(esc(ip) for ip in sample[:10])
                details_lines.append(f"• نمونه IPها: <code>{shown}</code>")
        else:
            gib = d.get("delta_gib")
            window = d.get("window_seconds")
            if gib is not None:
                details_lines.append(f"• مصرف: <b>{esc(gib)} GiB</b>")
            if window is not None:
                details_lines.append(f"• پنجره: {esc(window)} ثانیه")

        lines = [
            f"<b>{title}</b>",
            "",
            f"👤 یوزر قبلی: <code>{esc(e.email_original)}</code>",
            f"🔄 یوزر جدید: <code>{esc(e.email_new)}</code>",
            f"⛔ نوع: {esc(type_fa)}",
            f"📡 Inbound: <code>{esc(e.inbound_id)}</code> ({esc(e.protocol)})",
            f"🔑 {esc(e.credential_field.upper())} عوض شد: "
            f"<code>{esc(e.credential_old_masked)}</code> → "
            f"<code>{esc(e.credential_new_masked)}</code>",
        ]
        if details_lines:
            lines.append("")
            lines.append("<b>جزئیات:</b>")
            lines.extend(details_lines)
        lines.append("")
        lines.append(f"<b>دلیل:</b> <code>{esc(e.reason)}</code>")
        lines.append("")
        lines.append(f"<i>⏰ {esc(e.timestamp)}</i>")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Action engine
# ---------------------------------------------------------------------------


class ActionEngine:
    """Applies limits to the database, batches restarts, and emits events.

    The engine is the only component that writes. Both monitors call
    `queue_limit(...)` which is idempotent for already-limited or
    in-flight emails.
    """

    def __init__(
        self,
        config: Config,
        db: Database,
        event_store: EventStore,
        webhook: WebhookSender,
        telegram: TelegramSender,
    ) -> None:
        self.config = config
        self.db = db
        self.event_store = event_store
        self.webhook = webhook
        self.telegram = telegram
        self._pending: list[tuple[str, str, str, dict]] = []
        # In-flight dedup: emails queued for the current debounce window.
        # Cleared after every flush so a re-created (same-named) client can be
        # limited again in the future.
        self._inflight_emails: set[str] = set()
        self._lock = asyncio.Lock()
        self._debounce_task: Optional[asyncio.Task] = None

    def is_whitelisted(self, email: str) -> bool:
        wl = self.config.whitelist
        if email in wl.emails:
            return True
        for prefix in wl.name_prefixes:
            if prefix and email.startswith(prefix):
                return True
        for suffix in wl.suffix_blocklist:
            if suffix and email.endswith(suffix):
                return True  # already limited - skip
        return False

    async def queue_limit(
        self,
        email: str,
        limit_type: str,
        reason: str,
        details: Optional[dict] = None,
    ) -> None:
        if self.is_whitelisted(email):
            log.debug("skipping limit for whitelisted/already-limited email=%s", email)
            return

        suffix = (
            self.config.ip_limit.suffix
            if limit_type == "ip_limit"
            else self.config.traffic_limit.suffix
        )

        async with self._lock:
            if email in self._inflight_emails:
                return
            self._inflight_emails.add(email)
            self._pending.append((email, limit_type, reason, details or {}))
            log.info(
                "queued %s for %s: %s (pending=%d)",
                limit_type,
                email,
                reason,
                len(self._pending),
            )
            if self._debounce_task is None or self._debounce_task.done():
                self._debounce_task = asyncio.create_task(self._debounced_flush())

    async def _debounced_flush(self) -> None:
        await asyncio.sleep(self.config.restart.debounce_seconds)
        async with self._lock:
            batch = self._pending[:]
            self._pending.clear()
            self._inflight_emails.clear()
        if not batch:
            return
        events: list[LimitEvent] = []
        for email, limit_type, reason, details in batch:
            evt = await self._apply_one(email, limit_type, reason, details)
            if evt is not None:
                events.append(evt)
        if events:
            await self._restart_xui()
            for evt in events:
                await self.event_store.add(evt)
                asyncio.create_task(self.webhook.send(evt))
                asyncio.create_task(self.telegram.send_event(evt))

    async def _apply_one(
        self,
        email: str,
        limit_type: str,
        reason: str,
        details: dict,
    ) -> Optional[LimitEvent]:
        record = await asyncio.to_thread(self.db.find_client, email)
        if record is None:
            log.warning("client %s not found in any inbound - skipping", email)
            return None

        suffix = (
            self.config.ip_limit.suffix
            if limit_type == "ip_limit"
            else self.config.traffic_limit.suffix
        )
        new_email = self._pick_new_email(email, suffix)
        new_credential = self._new_credential(record)
        note = f"LIMITED({limit_type}) at {datetime.now(timezone.utc).isoformat()}: {reason}"

        try:
            await asyncio.to_thread(
                self.db.apply_limit, record, new_email, new_credential, note
            )
        except Exception as exc:
            log.exception("failed to apply %s for %s: %s", limit_type, email, exc)
            return None

        log.warning(
            "APPLIED %s: %s -> %s (inbound=%d protocol=%s) reason=%s",
            limit_type.upper(),
            email,
            new_email,
            record.inbound_id,
            record.protocol,
            reason,
        )

        return LimitEvent(
            id=f"evt_{int(time.time()*1000)}_{uuid.uuid4().hex[:8]}",
            timestamp=datetime.now(timezone.utc).isoformat(),
            type=limit_type,
            email_original=email,
            email_new=new_email,
            inbound_id=record.inbound_id,
            protocol=record.protocol,
            credential_field=record.credential_field,
            credential_old_masked=_mask_credential(record.credential_value),
            credential_new_masked=_mask_credential(new_credential),
            reason=reason,
            details=details,
        )

    def _pick_new_email(self, email: str, suffix: str) -> str:
        candidate = f"{email}{suffix}"
        if not self.db.email_exists(candidate):
            return candidate
        return f"{email}{suffix}-{int(time.time())}"

    def _new_credential(self, record: ClientRecord) -> str:
        if record.credential_field == "id":
            return str(uuid.uuid4())
        if record.protocol == "shadowsocks":
            # 32 random bytes -> base64 (matches xray expectation for SS2022)
            raw = os.urandom(32)
            return base64.b64encode(raw).decode("ascii")
        # trojan or generic password
        alphabet = string.ascii_letters + string.digits
        return "".join(random.SystemRandom().choices(alphabet, k=32))

    async def _restart_xui(self) -> None:
        cmd = self.config.restart.command
        log.info("restarting x-ui: %s", " ".join(cmd))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                log.error(
                    "x-ui restart failed (rc=%d) stdout=%s stderr=%s",
                    proc.returncode,
                    stdout.decode(errors="replace")[:200],
                    stderr.decode(errors="replace")[:200],
                )
            else:
                log.info("x-ui restart ok")
        except Exception as exc:
            log.exception("x-ui restart raised: %s", exc)


# ---------------------------------------------------------------------------
# IP monitor - tails xray access.log and tracks unique IPs per email
# ---------------------------------------------------------------------------


# Captures lines like:
#   2024/01/15 10:30:45 from 1.2.3.4:12345 accepted tcp:foo:443 [in -> out] email: user1
#   2024/01/15 10:30:45 from 1.2.3.4:0 accepted ... [in >> direct] email: partner_x | MELISA-y
# IPv6 source: from [2001:db8::1]:12345
# The email field is captured greedily to end-of-line because 3x-ui clients
# often have spaces / pipes / non-ASCII in their email field.
ACCESS_LOG_RE = re.compile(
    r"from\s+(?:\[(?P<ip6>[0-9a-fA-F:]+)\]|(?P<ip4>[0-9.]+)):\d+\s+"
    r"accepted\b.*?"
    r"email:\s*(?P<email>.+)$"
)


class IpMonitor:
    def __init__(self, config: Config, actions: ActionEngine) -> None:
        self.config = config
        self.actions = actions
        # email -> deque[(timestamp, ip)]
        self._activity: dict[str, deque[tuple[float, str]]] = defaultdict(deque)
        # email -> timestamp when it first exceeded the threshold
        self._exceeded_since: dict[str, float] = {}

    async def run(self) -> None:
        cfg = self.config.ip_limit
        if not cfg.enabled:
            log.info("ip_limit disabled")
            return
        log.info(
            "ip_limit watching %s (max_ips=%d window=%ds sustain=%ds)",
            cfg.access_log,
            cfg.max_ips,
            cfg.window_seconds,
            cfg.sustain_seconds,
        )
        async for line in self._tail(cfg.access_log):
            self._process_line(line)

    async def _tail(self, path: str):
        """Async generator yielding new lines from `path`, surviving rotation."""
        last_inode: Optional[int] = None
        f = None
        try:
            while True:
                try:
                    stat = os.stat(path)
                except FileNotFoundError:
                    if f is not None:
                        f.close()
                        f = None
                        last_inode = None
                    log.warning(
                        "access log %s missing - waiting (enable xray access log in 3x-ui)",
                        path,
                    )
                    await asyncio.sleep(10)
                    continue

                if stat.st_ino != last_inode:
                    if f is not None:
                        f.close()
                    f = open(path, "r", encoding="utf-8", errors="replace")
                    if last_inode is None:
                        # First open: skip to end so we don't replay old history
                        f.seek(0, os.SEEK_END)
                    last_inode = stat.st_ino
                    log.info("opened access log inode=%d", last_inode)

                line = f.readline()
                if not line:
                    # Detect truncation (logrotate copytruncate): file shrunk
                    # under us. New content is at the start, so seek(0).
                    try:
                        if os.fstat(f.fileno()).st_size < f.tell():
                            f.seek(0)
                    except OSError:
                        pass
                    await asyncio.sleep(0.5)
                    continue
                yield line
        finally:
            if f is not None:
                f.close()

    def _process_line(self, line: str) -> None:
        m = ACCESS_LOG_RE.search(line)
        if not m:
            return
        ip = m.group("ip6") or m.group("ip4")
        email = m.group("email").strip().rstrip(",;")
        if not ip or not email:
            return
        now = time.time()
        cfg = self.config.ip_limit

        bucket = self._activity[email]
        bucket.append((now, ip))
        cutoff = now - cfg.window_seconds
        while bucket and bucket[0][0] < cutoff:
            bucket.popleft()

        unique_ips = {entry[1] for entry in bucket}
        if len(unique_ips) > cfg.max_ips:
            first_seen = self._exceeded_since.get(email)
            if first_seen is None:
                self._exceeded_since[email] = now
                log.info(
                    "email=%s started exceeding ip limit: %d unique IPs in window",
                    email,
                    len(unique_ips),
                )
                return
            if now - first_seen >= cfg.sustain_seconds:
                reason = (
                    f"Exceeded {cfg.max_ips} concurrent IPs "
                    f"(observed {len(unique_ips)} unique IPs in last {cfg.window_seconds}s, "
                    f"sustained for {int(now - first_seen)}s)"
                )
                details = {
                    "ip_count": len(unique_ips),
                    "ips_sample": sorted(unique_ips)[:20],
                    "window_seconds": cfg.window_seconds,
                    "sustained_seconds": int(now - first_seen),
                }
                self._exceeded_since.pop(email, None)
                # bucket cleared so we don't immediately retrigger before xui restart
                self._activity.pop(email, None)
                asyncio.create_task(
                    self.actions.queue_limit(email, "ip_limit", reason, details)
                )
        else:
            # Below threshold again - reset sustain timer
            self._exceeded_since.pop(email, None)


# ---------------------------------------------------------------------------
# Traffic monitor - snapshots client_traffics and flags abuse
# ---------------------------------------------------------------------------


class TrafficMonitor:
    def __init__(self, config: Config, db: Database, actions: ActionEngine) -> None:
        self.config = config
        self.db = db
        self.actions = actions
        # rolling buffer of (timestamp, {email: cumulative_bytes})
        self._snapshots: deque[tuple[float, dict[str, int]]] = deque()

    async def run(self) -> None:
        cfg = self.config.traffic_limit
        if not cfg.enabled:
            log.info("traffic_limit disabled")
            return
        log.info(
            "traffic_limit polling every %ds, window=%ds, threshold=%d bytes (%.2f GiB)",
            cfg.snapshot_interval_seconds,
            cfg.comparison_window_seconds,
            cfg.threshold_bytes,
            cfg.threshold_bytes / (1024**3),
        )
        # Initial snapshot to seed the window
        await self._tick()
        while True:
            await asyncio.sleep(cfg.snapshot_interval_seconds)
            await self._tick()

    async def _tick(self) -> None:
        cfg = self.config.traffic_limit
        try:
            snapshot_raw = await asyncio.to_thread(self.db.list_client_traffics)
        except sqlite3.OperationalError as exc:
            log.warning("traffic snapshot db error (will retry): %s", exc)
            return
        except Exception as exc:
            log.exception("traffic snapshot unexpected error: %s", exc)
            return

        now = time.time()
        # Reduce snapshot to email -> bytes (enable status checked separately)
        snap = {email: total for email, (total, _enable) in snapshot_raw.items()}
        self._snapshots.append((now, snap))

        # Trim snapshots older than window + buffer (keep one beyond window
        # so we can always find a comparison point near the lower edge)
        keep_until = now - cfg.comparison_window_seconds - cfg.snapshot_interval_seconds * 2
        while len(self._snapshots) > 2 and self._snapshots[0][0] < keep_until:
            self._snapshots.popleft()

        old_snap = self._find_window_snapshot(now - cfg.comparison_window_seconds)
        if old_snap is None:
            return

        for email, current_bytes in snap.items():
            old_bytes = old_snap.get(email)
            if old_bytes is None:
                continue
            delta = current_bytes - old_bytes
            if delta <= 0:
                continue  # counter reset or no traffic
            if delta < cfg.threshold_bytes:
                continue
            # Make sure the row is still enabled (3x-ui marks disabled clients
            # too, no point limiting an already-disabled one)
            enable = snapshot_raw[email][1]
            if enable == 0:
                continue
            reason = (
                f"Traffic abuse: {delta / (1024**3):.2f} GiB in "
                f"{cfg.comparison_window_seconds}s window "
                f"(threshold {cfg.threshold_bytes / (1024**3):.2f} GiB)"
            )
            details = {
                "delta_bytes": delta,
                "delta_gib": round(delta / (1024**3), 3),
                "window_seconds": cfg.comparison_window_seconds,
                "old_total_bytes": old_bytes,
                "current_total_bytes": current_bytes,
            }
            asyncio.create_task(
                self.actions.queue_limit(email, "traffic_limit", reason, details)
            )

    def _find_window_snapshot(self, target_ts: float) -> Optional[dict[str, int]]:
        """Pick the oldest snapshot that is still >= target_ts.

        i.e. the snapshot closest to "window_seconds ago" but not older than
        that, so the measured delta covers >= window_seconds of real time.
        """
        for ts, snap in self._snapshots:
            if ts >= target_ts:
                return snap
        return None


# ---------------------------------------------------------------------------
# HTTP API - lets external systems poll the event log
# ---------------------------------------------------------------------------


class ApiServer:
    def __init__(self, config: Config, event_store: EventStore) -> None:
        self.config = config
        self.event_store = event_store
        self.app = web.Application()
        self.app.router.add_get("/healthz", self._healthz)
        self.app.router.add_get("/events", self._auth(self._list_events))
        self._runner: Optional[web.AppRunner] = None

    def _auth(self, handler):
        token = self.config.api.token

        async def wrapper(request: web.Request) -> web.Response:
            provided = request.headers.get("Authorization", "")
            if provided.startswith("Bearer "):
                provided = provided[7:]
            if not token or provided != token:
                return web.json_response({"error": "unauthorized"}, status=401)
            return await handler(request)

        return wrapper

    async def _healthz(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "time": datetime.now(timezone.utc).isoformat()})

    async def _list_events(self, request: web.Request) -> web.Response:
        since = request.query.get("since")
        events = await self.event_store.list_after(since)
        return web.json_response({"events": [e.to_public_dict() for e in events]})

    async def start(self) -> None:
        if not self.config.api.enabled:
            log.info("api disabled")
            return
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.config.api.host, self.config.api.port)
        await site.start()
        log.info(
            "api listening on http://%s:%d (token required)",
            self.config.api.host,
            self.config.api.port,
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def setup_logging(cfg: LoggingConfig) -> None:
    level = getattr(logging, cfg.level.upper(), logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if cfg.file:
        try:
            Path(cfg.file).parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                cfg.file,
                maxBytes=cfg.max_size_mb * 1024 * 1024,
                backupCount=cfg.backup_count,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except Exception as exc:
            log.warning("could not open log file %s: %s", cfg.file, exc)


async def amain(config_path: str) -> int:
    config = Config.load(config_path)
    setup_logging(config.logging)
    log.info("xui-limiter starting (config=%s)", config_path)

    db = Database(config.database_path)
    event_store = EventStore(capacity=config.api.retention_events)
    webhook = WebhookSender(config.webhook)
    telegram = TelegramSender(config.telegram)
    actions = ActionEngine(config, db, event_store, webhook, telegram)
    if telegram.is_active():
        log.info(
            "telegram notifications on (chat_id=%s)",
            config.telegram.chat_id,
        )
        # Fire-and-forget startup ping so the user sees the bot is wired up.
        asyncio.create_task(
            telegram.send_raw(
                "🟢 <b>xui-limiter آنلاین شد</b>\n"
                f"<i>{datetime.now(timezone.utc).isoformat()}</i>"
            )
        )

    ip_monitor = IpMonitor(config, actions)
    traffic_monitor = TrafficMonitor(config, db, actions)
    api = ApiServer(config, event_store)

    await api.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # Windows for local dev
            pass

    tasks: list[asyncio.Task] = []
    if config.ip_limit.enabled:
        tasks.append(asyncio.create_task(ip_monitor.run(), name="ip_monitor"))
    if config.traffic_limit.enabled:
        tasks.append(asyncio.create_task(traffic_monitor.run(), name="traffic_monitor"))

    if not tasks:
        log.error("nothing to do: both ip_limit and traffic_limit are disabled")
        await api.stop()
        return 2

    stop_task = asyncio.create_task(stop_event.wait(), name="stop_signal")
    done, pending = await asyncio.wait(
        tasks + [stop_task], return_when=asyncio.FIRST_COMPLETED
    )

    for d in done:
        if d is stop_task:
            log.info("shutdown signal received")
        else:
            exc = d.exception()
            if exc:
                log.exception("task %s crashed", d.get_name(), exc_info=exc)

    for p in pending:
        p.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    await api.stop()
    log.info("xui-limiter stopped")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="3x-ui advanced client limiter")
    parser.add_argument(
        "-c",
        "--config",
        default="/etc/xui-limiter/config.yaml",
        help="Path to config.yaml",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate config and exit",
    )
    args = parser.parse_args()

    if args.validate:
        try:
            cfg = Config.load(args.config)
            db = Database(cfg.database_path)
            # quick sanity probe
            _ = db.list_client_traffics()
            print("config OK")
            print(f"  database: {cfg.database_path}")
            print(f"  ip_limit: {'on' if cfg.ip_limit.enabled else 'off'}")
            print(f"  traffic_limit: {'on' if cfg.traffic_limit.enabled else 'off'}")
            print(f"  webhook: {'on' if cfg.webhook.enabled else 'off'}")
            print(f"  api: {'on' if cfg.api.enabled else 'off'}")
            return 0
        except Exception as exc:
            print(f"config INVALID: {exc}", file=sys.stderr)
            return 1

    try:
        return asyncio.run(amain(args.config))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
