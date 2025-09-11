from .user_agent_patch import UserAgentPatch
# from .scrape_request_paused_handler import ScrapeRequestPausedHandler
from .window_size_patch import WindowSizePatch
from .cloudflare_solver import CloudflareSolver

__all__ = [
    "UserAgentPatch",
    # "ScrapeRequestPausedHandler",
    "WindowSizePatch",
    "CloudflareSolver",
]