from .stealth_patch import StealthPatch, patch_stealth
from .user_agent_patch import UserAgentPatch, patch_user_agent
from .scrape_request_paused_handler import ScrapeRequestPausedHandler
from .window_size_patch import WindowSizePatch, patch_window_size

__all__ = [
    "StealthPatch",
    "UserAgentPatch",
    "patch_stealth",
    "patch_user_agent",
    "ScrapeRequestPausedHandler",
    "WindowSizePatch",
    "patch_window_size",
]