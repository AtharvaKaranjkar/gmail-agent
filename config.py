import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Desktop-based output paths ───────────────────────────────────────
DESKTOP = Path.home() / "Desktop"
PROJECT_DIR = DESKTOP / "gmail-agent-output"
TARGET_DIR = PROJECT_DIR / "target"
BROWSER_DATA_DIR = PROJECT_DIR / "browser_data"
DB_PATH = PROJECT_DIR / "gmail_agent.db"
DOWNLOADS_DIR = PROJECT_DIR / "downloads"

# Create dirs on import
TARGET_DIR.mkdir(parents=True, exist_ok=True)
BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

# ── Gmail credentials ────────────────────────────────────────────────
GMAIL_EMAIL = os.getenv("GMAIL_EMAIL", "")
GMAIL_PASSWORD = os.getenv("GMAIL_PASSWORD", "")

# ── LLM Configuration (three tiers) ─────────────────────────────────
# Each tier: model + base_url. If model is blank → tier is disabled.
# Agent uses the highest available tier for each task.

NIM_API_KEY = os.getenv("NIM_API_KEY", "")

# Normal — navigation, DOM decisions, basic tasks
NIM_NORMAL_MODEL = os.getenv("NIM_NORMAL_MODEL", "")
NIM_NORMAL_BASE_URL = os.getenv("NIM_NORMAL_BASE_URL", "https://integrate.api.nvidia.com/v1")

# Reasoning — summarization, categorization, complex analysis
NIM_REASONING_MODEL = os.getenv("NIM_REASONING_MODEL", "")
NIM_REASONING_BASE_URL = os.getenv("NIM_REASONING_BASE_URL", "https://integrate.api.nvidia.com/v1")

# Vision — screenshot analysis alongside DOM state
NIM_VISION_MODEL = os.getenv("NIM_VISION_MODEL", "")
NIM_VISION_BASE_URL = os.getenv("NIM_VISION_BASE_URL", "https://integrate.api.nvidia.com/v1")


def llm_available(tier: str) -> bool:
    """Check if a given LLM tier is configured."""
    models = {
        "normal": NIM_NORMAL_MODEL,
        "reasoning": NIM_REASONING_MODEL,
        "vision": NIM_VISION_MODEL,
    }
    return bool(NIM_API_KEY and models.get(tier, ""))


def get_llm_config(tier: str) -> dict | None:
    """
    Get model + base_url for a tier.
    Returns None if that tier is not configured.
    """
    tiers = {
        "normal":    (NIM_NORMAL_MODEL, NIM_NORMAL_BASE_URL),
        "reasoning": (NIM_REASONING_MODEL, NIM_REASONING_BASE_URL),
        "vision":    (NIM_VISION_MODEL, NIM_VISION_BASE_URL),
    }
    model, base_url = tiers.get(tier, ("", ""))
    if not model or not NIM_API_KEY:
        return None
    return {"model": model, "base_url": base_url, "api_key": NIM_API_KEY}


def resolve_llm(preferred: str, fallback_order: list[str] | None = None) -> dict | None:
    """
    Try to get the preferred tier. If unavailable, walk the fallback list.

    Usage:
        cfg = resolve_llm("vision", ["reasoning", "normal"])
        # tries vision first, then reasoning, then normal
        # returns None only if ALL are blank
    """
    if fallback_order is None:
        fallback_order = []

    for tier in [preferred] + fallback_order:
        cfg = get_llm_config(tier)
        if cfg:
            return cfg
    return None


# ── Agent settings ───────────────────────────────────────────────────
THREAD_COUNT_THRESHOLD = int(os.getenv("THREAD_COUNT_THRESHOLD", "4"))
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"

# ── Browser behaviour ───────────────────────────────────────────────
VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 900
WAIT_AFTER_ACTION = 0.8
WAIT_FOR_NETWORK_IDLE = 1.0
MAX_FAILURES_PER_STEP = 3
MAX_STEPS = 200
MAX_ACTIONS_PER_STEP = 3

# ── Allowed attachment extensions ────────────────────────────────────
ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".pdf", ".docx", ".xlsx"}

# ── DOM extraction settings ─────────────────────────────────────────
INCLUDE_ATTRIBUTES = [
    "id", "title", "type", "name", "role",
    "aria-label", "placeholder", "value", "alt",
    "aria-expanded", "data-tooltip", "href",
]