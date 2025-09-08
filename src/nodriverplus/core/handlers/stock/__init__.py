from .user_agent_patch import UserAgentPatch, patch_user_agent
# from .scrape_request_paused_handler import ScrapeRequestPausedHandler
from .window_size_patch import WindowSizePatch, patch_window_size
from .cloudflare_solver import CloudflareSolver

__all__ = [
    "UserAgentPatch",
    "patch_user_agent",
    # "ScrapeRequestPausedHandler",
    "WindowSizePatch",
    "patch_window_size",
    "CloudflareSolver",
]