from .targets import (
    TargetInterceptor,
    TargetInterceptorManager,
)
from .stock import (
    UserAgentPatch,
    patch_user_agent,
    StealthPatch,
    apply_stealth,
    ScrapeRequestPausedHandler
)

__all__ = [
    "UserAgentPatch",
    "patch_user_agent",
    "StealthPatch",
    "apply_stealth",
    "TargetInterceptor",
    "TargetInterceptorManager",
    "ScrapeRequestPausedHandler"
]