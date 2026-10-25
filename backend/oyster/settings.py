"""Backend configuration, loaded from environment / backend/.env (see .env.example).

Secrets (AI_API_KEY, OYSTER_ADMIN_TOKEN, LAB_FIXTURE_AUTH_TOKEN) are read here and never logged;
the Settings repr omits them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    v = os.getenv(name)
    return int(v) if v not in (None, "") else default


def _float_or_none(name: str) -> float | None:
    v = os.getenv(name)
    return float(v) if v not in (None, "") else None


@dataclass(frozen=True)
class RunBudgets:
    max_actions: int = 12
    max_provider_requests: int = 6
    max_duration_s: int = 180
    max_confirmation_wait_s: int = 120


@dataclass(frozen=True)
class RunLimits:
    """Admission limits for AI runs (each run can spend provider tokens and drive the device)."""

    max_active_per_session: int = 1  # queued/running/awaiting_* runs of one session
    max_queued_total: int = 4  # queued runs across all sessions
    max_per_device_per_hour: int = 30  # runs created by one device's sessions in the last hour


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    bind_host: str
    https_port: int
    http_port: int
    admin_token: str = field(repr=False)
    tls_cert: Path
    tls_key: Path
    ai_provider: str
    ai_base_url: str
    ai_model: str
    ai_api_key: str = field(repr=False)
    ai_timeout_s: int
    ai_max_input_tokens: int
    ai_max_output_tokens: int
    ai_price_input_per_mtok: float | None
    ai_price_output_per_mtok: float | None
    budgets: RunBudgets = field(default_factory=RunBudgets)
    run_limits: RunLimits = field(default_factory=RunLimits)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "oyster.db"

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def provider_is_fake(self) -> bool:
        return self.ai_provider == "fake"

    def pricing_configured(self) -> bool:
        return self.ai_price_input_per_mtok is not None and self.ai_price_output_per_mtok is not None


def load_settings(env_file: str | os.PathLike | None = None) -> Settings:
    load_dotenv(env_file or (BACKEND_DIR / ".env"), override=False)
    data_dir = Path(os.getenv("OYSTER_DATA_DIR", str(BACKEND_DIR / "data")))
    if not data_dir.is_absolute():
        data_dir = (BACKEND_DIR / data_dir).resolve()
    return Settings(
        data_dir=data_dir,
        # all interfaces on purpose: the Cardputer reaches the HTTPS listener over the LAN (ADR-003);
        # the owner HTTP listener is always 127.0.0.1
        bind_host=os.getenv("OYSTER_BIND_HOST", "0.0.0.0"),  # noqa: S104 - intentional LAN bind
        https_port=_int("OYSTER_HTTPS_PORT", 8443),
        http_port=_int("OYSTER_HTTP_PORT", 8000),
        admin_token=os.getenv("OYSTER_ADMIN_TOKEN", ""),
        tls_cert=Path(os.getenv("OYSTER_TLS_CERT", str(data_dir / "dev-ca" / "server.crt"))),
        tls_key=Path(os.getenv("OYSTER_TLS_KEY", str(data_dir / "dev-ca" / "server.key"))),
        ai_provider=os.getenv("AI_PROVIDER", "groq"),
        ai_base_url=os.getenv("AI_BASE_URL", "https://api.groq.com/openai/v1"),
        ai_model=os.getenv("AI_MODEL", ""),
        ai_api_key=os.getenv("AI_API_KEY", ""),
        ai_timeout_s=_int("AI_TIMEOUT_S", 60),
        ai_max_input_tokens=_int("AI_MAX_INPUT_TOKENS", 12000),
        ai_max_output_tokens=_int("AI_MAX_OUTPUT_TOKENS", 1500),
        ai_price_input_per_mtok=_float_or_none("AI_PRICE_INPUT_PER_MTOK"),
        ai_price_output_per_mtok=_float_or_none("AI_PRICE_OUTPUT_PER_MTOK"),
        budgets=RunBudgets(
            max_actions=_int("RUN_MAX_ACTIONS", 12),
            max_provider_requests=_int("RUN_MAX_PROVIDER_REQUESTS", 6),
            max_duration_s=_int("RUN_MAX_DURATION_S", 180),
            max_confirmation_wait_s=_int("RUN_MAX_CONFIRMATION_WAIT_S", 120),
        ),
        run_limits=RunLimits(
            max_active_per_session=_int("RUN_LIMIT_ACTIVE_PER_SESSION", 1),
            max_queued_total=_int("RUN_LIMIT_QUEUED_TOTAL", 4),
            max_per_device_per_hour=_int("RUN_LIMIT_PER_DEVICE_HOUR", 30),
        ),
    )
