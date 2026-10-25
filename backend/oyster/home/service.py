"""Smart Home rules: what the device may list, read and switch, and what gets recorded.

The order of an action request (each step can end it, nothing after it runs):
  1. per-device rate limit                                 429
  2. configuration complete (URL, token, allowlist)        503 home_not_configured          (audited)
  3. entity in the allowlist                               404 not_found                    (audited)
  4. action in the entity's actions                        403 action_not_allowed           (audited)
  5. impactful entity without `confirmed: true`            409 confirmation_required        (audited)
  6. Idempotency-Key already used: same request -> the stored answer (duplicate: true), other -> 409
  7. a "pending" audit row (no row, no action), then the Home Assistant call, then the outcome row.
`confirmed` is a claim of the caller: the confirmation itself is the key press on the Cardputer, and only an
authenticated device can send it (the API refuses the owner token on this route).

Not verified against a real Home Assistant instance: the tests use a fake one (docs/HOME_ASSISTANT.md).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from oyster.api.errors import ApiError
from oyster.deck.jobs import RateLimiter
from oyster.home.adapter import EntityMissing, HomeAssistantClient, HomeError, clean_state
from oyster.home.allowlist import ACTIONS, ENTITY_ID, ROOM_ID, Allowlist, AllowlistError, load_allowlist
from oyster.home.config import HomeConfig
from oyster.home.store import AuditEntry, HomeStore, fingerprint, key_hash

log = logging.getLogger("oyster.home")

ROOMS_PAGE_MAX = 16
DEVICES_PAGE_MAX = 8  # worst case ~230 bytes per device: a page stays well under the 4 KiB response bound
KEY_PATTERN = r"^[A-Za-z0-9._:-]{8,80}$"


@dataclass(frozen=True)
class Configuration:
    configured: bool
    reason: str | None = None
    allowlist: Allowlist | None = None
    problems: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Actor:
    kind: str  # "device" | "owner"
    id: str
    device_id: str | None

    @property
    def rate_key(self) -> str:
        return self.device_id or "owner"


def _home_api_error(e: HomeError) -> ApiError:
    return ApiError(e.status, e.code, e.detail)


class HomeService:
    def __init__(
        self,
        config: HomeConfig,
        store: HomeStore,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        transport: httpx.BaseTransport | None = None,
        actions_per_minute: int = 20,
        reads_per_minute: int = 60,
        state_budget_s: float = 8.0,
        probe_ttl_s: float = 10.0,
    ):
        self._cfg = config
        self.store = store
        self._clock = clock
        self._mono = monotonic
        self._transport = transport
        self._state_budget_s = state_budget_s
        self._probe_ttl_s = probe_ttl_s
        self._action_limiter = RateLimiter(actions_per_minute, 60.0, clock)
        self._read_limiter = RateLimiter(reads_per_minute, 60.0, clock)
        self._lock = threading.Lock()
        self._client: HomeAssistantClient | None = None
        self._sig: tuple[int, int] | None | str = "unread"
        self._loaded: Configuration | None = None
        self._probed_until = 0.0
        self._inflight: set[tuple[str, str]] = set()

    # ---- configuration ---------------------------------------------------------------------------------

    def configuration(self) -> Configuration:
        problem = self._cfg.problem()
        if problem is not None:
            return Configuration(False, problem)
        path = self._cfg.allowlist_path
        try:
            st = path.stat()
            sig: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
        except OSError:
            sig = None
        with self._lock:
            if self._loaded is not None and self._sig == sig:
                return self._loaded
            try:
                al = load_allowlist(path)
                loaded = Configuration(True, None, al)
                log.info("home allowlist loaded: %d room(s), %d entit(ies)", len(al.rooms), al.entity_count)
            except AllowlistError as e:
                loaded = Configuration(False, e.reason, None, e.problems)
                if e.reason != "no_allowlist":
                    log.warning("home allowlist rejected (%s): %d problem(s)", e.reason, len(e.problems))
            self._sig, self._loaded = sig, loaded
            return loaded

    def status(self) -> dict:
        """Owner view: what is configured and why not, never the URL or the token."""
        cfg = self.configuration()
        al = cfg.allowlist
        return {
            "configured": cfg.configured,
            "reason": cfg.reason,
            "url_set": bool(self._cfg.url),
            "token_set": bool(self._cfg.token),
            "tls": self._cfg.tls,
            "ca_file_set": bool(self._cfg.ca_file),
            "allowlist_file": self._cfg.allowlist_path.name,
            "rooms": len(al.rooms) if al else 0,
            "entities": al.entity_count if al else 0,
            "impactful": al.impactful_count if al else 0,
            "problems": cfg.problems,
        }

    def close(self) -> None:
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            client.close()

    def _ha(self) -> HomeAssistantClient:
        with self._lock:
            if self._client is None:
                try:
                    self._client = HomeAssistantClient(self._cfg, transport=self._transport)
                except (OSError, ValueError):
                    raise HomeError(
                        "home_unreachable", 503, "Home Assistant cannot be reached: check its CA file"
                    ) from None
            return self._client

    def _probe(self) -> None:
        """Reachability and token check, remembered for a few seconds after a success."""
        now = self._mono()
        if now < self._probed_until:
            return
        self._ha().ping()
        self._probed_until = now + self._probe_ttl_s

    def _throttle(self, limiter: RateLimiter, actor: Actor) -> None:
        wait = limiter.check(actor.rate_key)
        if wait is not None:
            raise ApiError(
                429, "rate_limited", f"too many requests; retry in {wait} s", {"Retry-After": str(wait)}
            )

    def _audit(self, e: AuditEntry, *, required: bool = False) -> None:
        try:
            self.store.add_audit(e)
        except (sqlite3.Error, OSError):  # the text could name a path, so it is not logged
            log.error("home audit write failed")
            if required:
                raise ApiError(
                    503, "audit_unavailable", "the audit log cannot be written; nothing was done"
                ) from None

    def _entry(self, actor: Actor, event: str, outcome: str, **kw) -> AuditEntry:
        return AuditEntry(
            self._clock(), actor.kind, actor.id, event, outcome, device_id=actor.device_id, **kw
        )

    # ---- reads -----------------------------------------------------------------------------------------

    def rooms(self, actor: Actor, after: str | None, limit: int | None) -> dict:
        self._throttle(self._read_limiter, actor)
        cfg = self.configuration()
        if not cfg.configured or cfg.allowlist is None:
            return {"configured": False, "reason": cfg.reason, "rooms": [], "next_after": None}
        try:
            self._probe()
        except HomeError as e:
            raise _home_api_error(e) from None
        start, size = int(after or 0), min(limit or ROOMS_PAGE_MAX, ROOMS_PAGE_MAX)
        rooms = cfg.allowlist.rooms
        page = rooms[start : start + size]
        nxt = str(start + size) if start + size < len(rooms) else None
        return {
            "configured": True,
            "rooms": [{"id": r.room_id, "name": r.name, "count": len(r.entities)} for r in page],
            "next_after": nxt,
        }

    def devices(self, actor: Actor, room_id: str, after: str | None, limit: int | None) -> dict:
        self._throttle(self._read_limiter, actor)
        cfg = self.configuration()
        if not cfg.configured or cfg.allowlist is None:
            raise ApiError(503, "home_not_configured", "Home Assistant is not set up")
        room = cfg.allowlist.room(room_id) if ROOM_ID.fullmatch(room_id) else None
        if room is None:
            self._audit(
                self._entry(
                    actor,
                    "denied",
                    "denied",
                    reason="unknown_room",
                    room_id=room_id if ROOM_ID.fullmatch(room_id) else None,
                )
            )
            raise ApiError(404, "not_found", "unknown room")
        start, size = int(after or 0), min(limit or DEVICES_PAGE_MAX, DEVICES_PAGE_MAX)
        page = room.entities[start : start + size]
        nxt = str(start + size) if start + size < len(room.entities) else None
        try:
            client = self._ha()
            deadline = self._mono() + self._state_budget_s
            devices = []
            for e in page:
                if e.domain == "scene":
                    state = "scene"  # a scene's state is the time it was last used: not worth showing
                elif self._mono() > deadline:
                    state = "unknown"  # out of time budget: say so rather than wait for every entity
                else:
                    try:
                        state = client.get_state(e.entity_id)
                    except EntityMissing:
                        state = "missing"
                devices.append(
                    {
                        "entity_id": e.entity_id,
                        "label": e.label,
                        "state": state,
                        "impactful": e.impactful,
                        "actions": list(e.actions),
                    }
                )
        except HomeError as e:
            raise _home_api_error(e) from None
        return {"room": {"id": room.room_id, "name": room.name}, "devices": devices, "next_after": nxt}

    # ---- actions ---------------------------------------------------------------------------------------

    def act(self, actor: Actor, entity_id: str, action: str, confirmed: bool, key: str) -> dict:
        self._throttle(self._action_limiter, actor)
        if action not in ACTIONS:
            raise ApiError(422, "validation_failed", "action must be turn_on, turn_off or toggle")
        khash = key_hash(actor.id, key)
        safe_entity = entity_id if ENTITY_ID.fullmatch(entity_id) and len(entity_id) <= 64 else None

        def deny(status: int, code: str, detail: str, **kw) -> ApiError:
            self._audit(
                self._entry(
                    actor,
                    "denied",
                    "denied",
                    reason=code,
                    entity_id=safe_entity,
                    action=action,
                    key_hash=khash,
                    **kw,
                )
            )
            return ApiError(status, code, detail)

        cfg = self.configuration()
        if not cfg.configured or cfg.allowlist is None:
            raise deny(503, "home_not_configured", "Home Assistant is not set up")
        entity = cfg.allowlist.entity(entity_id)
        if entity is None:
            raise deny(404, "not_found", "unknown entity")
        if action not in ACTIONS or action not in entity.actions:
            raise deny(
                403,
                "action_not_allowed",
                "this action is not allowed for this entity",
                room_id=entity.room_id,
            )
        if entity.impactful and not confirmed:
            raise deny(
                409,
                "confirmation_required",
                "this entity needs the local confirmation on the device",
                room_id=entity.room_id,
            )

        fp = fingerprint(entity_id, action, confirmed)
        try:
            stored = self.store.get_replay(actor.id, khash, self._clock())
        except (sqlite3.Error, OSError):
            log.error("home replay lookup failed")
            raise ApiError(
                503, "audit_unavailable", "the action log cannot be read; nothing was done"
            ) from None
        if stored is not None:
            if stored[0] != fp:
                raise deny(
                    409, "idempotency_conflict", "this Idempotency-Key was used for a different request"
                )
            self._audit(
                self._entry(
                    actor,
                    "action",
                    "replayed",
                    room_id=entity.room_id,
                    entity_id=entity_id,
                    action=action,
                    confirmed=confirmed,
                    key_hash=khash,
                )
            )
            return {**json.loads(stored[1]), "duplicate": True}

        slot = (actor.id, khash)
        with self._lock:
            if slot in self._inflight:
                raise ApiError(
                    409, "request_in_progress", "the same request is still running", {"Retry-After": "1"}
                )
            self._inflight.add(slot)
        try:
            return self._execute(actor, entity, action, confirmed, khash, fp)
        finally:
            with self._lock:
                self._inflight.discard(slot)

    def _execute(self, actor: Actor, entity, action: str, confirmed: bool, khash: str, fp: str) -> dict:
        common = {
            "room_id": entity.room_id,
            "entity_id": entity.entity_id,
            "action": action,
            "confirmed": confirmed,
            "key_hash": khash,
        }
        self._audit(self._entry(actor, "action", "pending", **common), required=True)
        domain, service = entity.service_for(action)
        try:
            client = self._ha()
            state = client.call_service(domain, service, entity.entity_id)
            if domain == "scene":
                state = "activated"
            elif state is None:
                try:
                    state = client.get_state(entity.entity_id)
                except HomeError:
                    state = "unknown"  # the action went through; only the read-back failed
        except HomeError as e:
            outcome = "unknown_outcome" if e.unknown_outcome else "failed"
            self._audit(self._entry(actor, "action", outcome, reason=e.code, **common))
            raise _home_api_error(e) from None
        answer = {
            "entity_id": entity.entity_id,
            "action": action,
            "result": "ok",
            "state": clean_state(state),
            "duplicate": False,
        }
        try:
            self.store.put_replay(
                actor.id, khash, fp, json.dumps({**answer, "duplicate": False}), self._clock()
            )
        except (sqlite3.Error, OSError):  # the action already happened; losing the replay record is not fatal
            log.error("home replay record write failed")
        self._audit(self._entry(actor, "action", "ok", **common))
        return answer
