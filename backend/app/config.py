"""Application configuration.

All runtime configuration is read from environment variables (optionally via a
``.env`` file). Access settings through :func:`get_settings`, which caches a
single validated instance per process.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(BACKEND_ROOT.parent / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # --- Environment -----------------------------------------------------
    env: Literal["dev", "test", "prod"] = Field(default="dev", alias="GOVFLOW_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # --- LLM ---------------------------------------------------------------
    llm_provider: Literal["anthropic", "gemini"] = Field(default="gemini", alias="LLM_PROVIDER")
    gemini_api_key: SecretStr | None = Field(default=None, alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-flash-lite-latest", alias="GEMINI_MODEL")
    gemini_base_url: str = Field(
        default="https://generativelanguage.googleapis.com/v1beta/openai/",
        alias="GEMINI_BASE_URL",
    )
    anthropic_api_key: SecretStr | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-sonnet-5-5", alias="ANTHROPIC_MODEL")
    llm_timeout_seconds: float = Field(default=60.0, gt=0, alias="LLM_TIMEOUT_SECONDS")
    llm_max_retries: int = Field(default=3, ge=0, le=10, alias="LLM_MAX_RETRIES")
    llm_max_tokens: int = Field(default=2048, gt=0, alias="LLM_MAX_TOKENS")
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = Field(default="medium", alias="LLM_EFFORT")
    # Server-side refusal fallback (Claude API only); off for Bedrock/Vertex proxies.
    llm_refusal_fallback: bool = Field(default=True, alias="LLM_REFUSAL_FALLBACK")
    # USD per million tokens (claude-sonnet-5-5 list prices), used for cost
    # logging and the per-job ceiling.
    llm_input_cost_per_mtok: float = Field(default=2.0, ge=0, alias="LLM_INPUT_COST_PER_MTOK")
    llm_output_cost_per_mtok: float = Field(default=10.0, ge=0, alias="LLM_OUTPUT_COST_PER_MTOK")
    llm_cache_read_cost_per_mtok: float = Field(default=0.2, ge=0, alias="LLM_CACHE_READ_COST_PER_MTOK")
    web_search_cost_per_1k: float = Field(default=10.0, ge=0, alias="WEB_SEARCH_COST_PER_1K")

    # --- Service discovery -------------------------------------------------
    search_provider: Literal["anthropic", "tavily", "brave"] = Field(default="anthropic", alias="SEARCH_PROVIDER")
    tavily_api_key: SecretStr | None = Field(default=None, alias="TAVILY_API_KEY")
    brave_api_key: SecretStr | None = Field(default=None, alias="BRAVE_API_KEY")
    search_max_results: int = Field(default=8, ge=1, le=20, alias="SEARCH_MAX_RESULTS")
    resolver_max_candidates: int = Field(default=5, ge=1, le=10, alias="RESOLVER_MAX_CANDIDATES")
    resolver_min_confidence: float = Field(default=0.75, ge=0, le=1, alias="RESOLVER_MIN_CONFIDENCE")
    # Domains to treat as official besides .gov.in/.nic.in/.gov/gov.<cc>.
    extra_official_domains: Annotated[list[str], NoDecode] = Field(
        default_factory=list, alias="EXTRA_OFFICIAL_DOMAINS"
    )
    # Hosts allowed over plain http (the local testbed).
    insecure_allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1"], alias="INSECURE_ALLOWED_HOSTS"
    )

    # --- Polite web access ---------------------------------------------------
    http_timeout_seconds: float = Field(default=15.0, gt=0, alias="HTTP_TIMEOUT_SECONDS")
    http_user_agent: str = Field(
        default="Mozilla/5.0 (compatible; GovFlowAI/0.1; citizen-assist agent)", alias="HTTP_USER_AGENT"
    )
    action_delay_min_seconds: float = Field(default=1.0, ge=0, alias="ACTION_DELAY_MIN_SECONDS")
    action_delay_max_seconds: float = Field(default=3.0, ge=0, alias="ACTION_DELAY_MAX_SECONDS")

    # --- Form planner --------------------------------------------------------
    planner_max_form_pages: int = Field(default=8, ge=1, alias="PLANNER_MAX_FORM_PAGES")
    planner_max_info_pages: int = Field(default=4, ge=0, alias="PLANNER_MAX_INFO_PAGES")
    planner_max_clicks: int = Field(default=12, ge=0, alias="PLANNER_MAX_CLICKS")
    planner_page_timeout_seconds: float = Field(default=30.0, gt=0, alias="PLANNER_PAGE_TIMEOUT_SECONDS")

    # --- Speech-to-text ----------------------------------------------------
    transcriber: Literal["openai", "faster_whisper"] = Field(default="openai", alias="TRANSCRIBER")
    openai_api_key: SecretStr | None = Field(default=None, alias="OPENAI_API_KEY")
    whisper_model: str = Field(default="whisper-1", alias="WHISPER_MODEL")
    faster_whisper_model: str = Field(default="small", alias="FASTER_WHISPER_MODEL")

    # --- Intake ------------------------------------------------------------
    confidence_threshold: float = Field(default=0.85, ge=0, le=1, alias="CONFIDENCE_THRESHOLD")
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, gt=0, alias="MAX_UPLOAD_BYTES")

    # --- Agent limits ------------------------------------------------------
    agent_max_steps: int = Field(default=60, gt=0, alias="AGENT_MAX_STEPS")
    agent_max_cost_usd: float = Field(default=2.0, gt=0, alias="AGENT_MAX_COST_USD")
    agent_max_same_action_failures: int = Field(default=3, gt=0, alias="AGENT_MAX_SAME_ACTION_FAILURES")
    human_wait_timeout_seconds: int = Field(default=600, gt=0, alias="HUMAN_WAIT_TIMEOUT_SECONDS")
    # Extra domains the agent may visit besides the target portal's own host.
    extra_allowed_domains: Annotated[list[str], NoDecode] = Field(default_factory=list, alias="EXTRA_ALLOWED_DOMAINS")

    # --- Browser -----------------------------------------------------------
    browser_headless: bool = Field(default=True, alias="BROWSER_HEADLESS")
    browser_viewport_width: int = Field(default=1280, gt=0, alias="BROWSER_VIEWPORT_WIDTH")
    browser_viewport_height: int = Field(default=800, gt=0, alias="BROWSER_VIEWPORT_HEIGHT")
    screencast_quality: int = Field(default=60, ge=1, le=100, alias="SCREENCAST_QUALITY")
    screencast_max_fps: int = Field(default=10, ge=1, le=30, alias="SCREENCAST_MAX_FPS")

    # --- Testbed portals (evals/regression only, Phase 9) --------------------
    portal_scholarship_url: str = Field(default="http://localhost:5001", alias="PORTAL_SCHOLARSHIP_URL")
    portal_license_url: str = Field(default="http://localhost:5002", alias="PORTAL_LICENSE_URL")
    portal_subsidy_url: str = Field(default="http://localhost:5003", alias="PORTAL_SUBSIDY_URL")

    # --- Security / storage -----------------------------------------------
    # Comma-separated Fernet keys. The first encrypts; all are tried for
    # decryption, which allows key rotation. In dev, a key is generated and
    # persisted under data_dir if unset. In prod it is required.
    master_keys: SecretStr | None = Field(default=None, alias="GOVFLOW_MASTER_KEYS")
    data_dir: Path = Field(default=BACKEND_ROOT / "data", alias="GOVFLOW_DATA_DIR")

    # --- API ---------------------------------------------------------------
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"],
        alias="CORS_ORIGINS",
    )

    @field_validator(
        "extra_allowed_domains", "cors_origins", "extra_official_domains", "insecure_allowed_hosts", mode="before"
    )
    @classmethod
    def _split_csv(cls, v: object) -> object:
        # Accept "a,b,c" from env in addition to a JSON list.
        if isinstance(v, str):
            stripped = v.strip()
            if stripped.startswith("["):
                return json.loads(stripped)
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return v

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, v: str) -> str:
        level = v.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid LOG_LEVEL: {v}")
        return level

    @field_validator("data_dir")
    @classmethod
    def _absolute_data_dir(cls, v: Path) -> Path:
        return v if v.is_absolute() else (BACKEND_ROOT / v).resolve()

    @model_validator(mode="after")
    def _prod_requirements(self) -> "Settings":
        if self.env == "prod" and self.master_keys is None:
            raise ValueError("GOVFLOW_MASTER_KEYS must be set when GOVFLOW_ENV=prod")
        if self.action_delay_max_seconds < self.action_delay_min_seconds:
            raise ValueError("ACTION_DELAY_MAX_SECONDS must be >= ACTION_DELAY_MIN_SECONDS")
        return self

    # --- Derived paths -----------------------------------------------------
    @property
    def profiles_dir(self) -> Path:
        return self.data_dir / "profiles"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def screenshots_dir(self) -> Path:
        return self.data_dir / "screenshots"

    @property
    def browser_state_dir(self) -> Path:
        return self.data_dir / "browser_state"

    @property
    def receipts_dir(self) -> Path:
        return self.data_dir / "receipts"

    @property
    def checkpoint_db_path(self) -> Path:
        return self.data_dir / "checkpoints.sqlite"

    @property
    def dev_master_key_path(self) -> Path:
        return self.data_dir / ".master.key"

    @property
    def plan_cache_path(self) -> Path:
        return self.data_dir / "plan_cache.sqlite"

    @property
    def effective_provider(self) -> str:
        if self.llm_provider == "gemini":
            return "gemini"
        if self.gemini_api_key and (not self.anthropic_api_key or not self.anthropic_api_key.get_secret_value().strip()):
            return "gemini"
        return "anthropic"

    @property
    def testbed_portals(self) -> dict[str, str]:
        """Regression/eval testbed only; product code never reads this."""
        return {
            "scholarship": self.portal_scholarship_url,
            "license_renewal": self.portal_license_url,
            "subsidy": self.portal_subsidy_url,
        }

    def ensure_dirs(self) -> None:
        for d in (
            self.data_dir,
            self.profiles_dir,
            self.uploads_dir,
            self.screenshots_dir,
            self.browser_state_dir,
            self.receipts_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    def llm_cost_usd(
        self, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0,
        cache_write_tokens: int = 0, web_searches: int = 0,
    ) -> float:
        tokens = (
            input_tokens * self.llm_input_cost_per_mtok
            + output_tokens * self.llm_output_cost_per_mtok
            + cache_read_tokens * self.llm_cache_read_cost_per_mtok
            + cache_write_tokens * self.llm_input_cost_per_mtok * 1.25
        ) / 1_000_000
        return tokens + web_searches * self.web_search_cost_per_1k / 1000


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
