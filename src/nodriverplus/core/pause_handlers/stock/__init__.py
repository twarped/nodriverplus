from .stealth_patch import StealthPatch, apply_stealth
from .user_agent_patch import UserAgentPatch, patch_user_agent
from .scrape_request_paused_handler import ScrapeRequestPausedHandler

__all__ = [
    "StealthPatch",
    "UserAgentPatch",
    "apply_stealth",
    "patch_user_agent",
    "ScrapeRequestPausedHandler"
]