from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
STORAGE_DIR = ROOT_DIR / "storage"
RAW_DIR = STORAGE_DIR / "raw"
EXPORT_DIR = STORAGE_DIR / "exports"
DB_PATH = STORAGE_DIR / "pricing.db"


def _load_loose_env() -> None:
    """Load only dotenv-style assignments from the user's mixed .env file.

    The supplied .env contains documentation and code snippets after the first
    three assignments. We intentionally ignore non-assignment lines and never
    print or log values.
    """

    env_path = ROOT_DIR / ".env"
    if not env_path.exists():
        return
    try:
        for raw_line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
                continue
            # Only accept conventional dotenv scalar assignments. This avoids
            # accidentally treating pasted Python/cURL snippets as settings.
            value = value.strip().strip("'").strip('"')
            if key in {
                "CLAUDE_BASE_URL",
                "CLAUDE_API_KEY",
                "CLAUDE_MODEL",
                "ENABLE_LLM",
                "MANUAL_PRICING_RULES_JSON",
                "LLM_TIMEOUT_SECONDS",
                "LLM_MAX_CALLS",
                "LLM_MAX_CANDIDATES",
                "LLM_MAX_RETRIES",
                "LLM_MAX_PROMPT_CHARS",
                "LLM_MAX_TOKENS",
                "LLM_RERANK_MARGIN",
                "PRICE_DRIFT_WARNING_THRESHOLD",
                "DATABASE_URL",
                "APP_HOST",
                "APP_PORT",
                "CHATBOT_BASE_URL",
                "CHATBOT_API_KEY",
                "CHATBOT_MODEL",
                "ENABLE_CHATBOT",
            } and key not in os.environ:
                os.environ[key] = value
    except OSError:
        return


_load_loose_env()


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    """Read a bounded integer without making startup fail on bad env input."""

    try:
        value = int(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    """Read a bounded float without making startup fail on bad env input."""

    try:
        value = float(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


@dataclass(frozen=True)
class Settings:
    app_name: str = "DH M&E Pricing"
    # Bind all interfaces by default so LAN clients and a Cloudflare Tunnel can reach the app.
    host: str = os.getenv("APP_HOST", "0.0.0.0")
    port: int = int(os.getenv("APP_PORT", "3000") or 3000)
    database_url: str = os.getenv("DATABASE_URL", f"sqlite:///{DB_PATH.as_posix()}")
    claude_base_url: str = os.getenv("CLAUDE_BASE_URL", "")
    claude_api_key: str = os.getenv("CLAUDE_API_KEY", "")
    claude_model: str = os.getenv("CLAUDE_MODEL", "")
    enable_llm: bool = os.getenv("ENABLE_LLM", "false").lower() in {"1", "true", "yes", "on"}
    # LLM is an optional semantic fallback. Keep conservative defaults so a
    # normal deterministic pricing run never creates an unbounded API bill.
    llm_timeout_seconds: float = _env_float("LLM_TIMEOUT_SECONDS", 30.0, minimum=0.1)
    llm_max_calls: int = _env_int("LLM_MAX_CALLS", 20, minimum=0)
    llm_max_candidates: int = _env_int("LLM_MAX_CANDIDATES", 10, minimum=1)
    llm_max_retries: int = _env_int("LLM_MAX_RETRIES", 1, minimum=0)
    llm_max_prompt_chars: int = _env_int("LLM_MAX_PROMPT_CHARS", 12000, minimum=1000)
    llm_max_tokens: int = _env_int("LLM_MAX_TOKENS", 800, minimum=0)
    llm_rerank_margin: float = _env_float("LLM_RERANK_MARGIN", 0.045, minimum=0.0)
    # A current supplier price that differs materially from an exact historical
    # observation remains usable for review, but must not be silently treated
    # as equivalent. The threshold is configurable because each business may
    # have a different tolerance for negotiated/project-price drift.
    price_drift_warning_threshold: float = _env_float(
        "PRICE_DRIFT_WARNING_THRESHOLD", 0.25, minimum=0.0
    )
    # Landing-page assistant widget. Fully separate from the pricing engine's
    # AI boundary above: it never sees pricing/catalog data and must only
    # answer from its own knowledge-base file. Base URL/model are not
    # secrets; the API key must come from the environment and is never
    # logged or returned to the browser.
    chatbot_base_url: str = os.getenv("CHATBOT_BASE_URL", "https://api.deepseek.com")
    chatbot_api_key: str = os.getenv("CHATBOT_API_KEY", "")
    chatbot_model: str = os.getenv("CHATBOT_MODEL", "deepseek-v4-pro")
    enable_chatbot: bool = os.getenv("ENABLE_CHATBOT", "true").lower() in {"1", "true", "yes", "on"}


settings = Settings()


def ensure_directories() -> None:
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
