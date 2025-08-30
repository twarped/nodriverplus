from .targets import (
    TargetInterceptor,
    TargetInterceptorManager,
)
from .stock import (
    UserAgentPatch,
    patch_user_agent,
    StealthPatch,
    apply_stealth,
)

__all__ = [
    "UserAgentPatch",
    "patch_user_agent",
    "StealthPatch",
    "apply_stealth",
    "TargetInterceptor",
    "TargetInterceptorManager"
]