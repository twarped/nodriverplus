"""user agent container using cdp.emulation.UserAgentMetadata directly.

we removed the local mirror to avoid drift; conversion now happens in JS so
metadata can be fed into emulation.UserAgentMetadata.from_json with no tweaks.
"""

from nodriver import cdp


class UserAgent:
    """primary user agent container (raw UA string + platform + metadata).

    also exposes acceptLanguage and an appVersion if specified. 
    `app_version` defaults to `user_agent` (minus the "Mozilla/" prefix).

    used for both protocol overrides and runtime
    patch injection inside pages/workers.
    """

    def __init__(self, 
        user_agent: str,
        platform: str,
        language: str,
        metadata: cdp.emulation.UserAgentMetadata | None = None,
        app_version: str = None
    ):
        self.user_agent = user_agent
        self.platform = platform
        self.language = language
        self.metadata = metadata
        self.app_version = app_version or user_agent.removeprefix("Mozilla/")

    @classmethod
    def from_json(cls, data: dict):
        """construct a UserAgent from a dict produced by get_user_agent.js.

        expects keys: user_agent, platform, language, metadata (camelCase for metadata
        sub-keys matching emulation.UserAgentMetadata.from_json).
        silently ignores extra keys.
        """
        meta_raw = data.get("metadata")
        metadata = cdp.emulation.UserAgentMetadata.from_json(meta_raw) if meta_raw else None
        user_agent = data.get("userAgent", "")
        return cls(
            user_agent=user_agent,
            platform=data.get("platform", ""),
            language=data.get("language", ""),
            metadata=metadata,
            app_version=data.get("appVersion") or user_agent.removeprefix("Mozilla/"),
        )

    def _fix_bools(self, obj):
        # coerce python bools into lowercase js literals
        if isinstance(obj, bool):
            return "true" if obj else "false"
        if isinstance(obj, dict):
            return {k: self._fix_bools(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._fix_bools(v) for v in obj]
        return obj

    def to_json(self, include_app_version = False, fix_bools = False) -> dict[str, str | dict[str, str | bool | list[str] | list[dict[str, str]]]]:
        """serialize ua info for protocol overrides / patches.

        :param include_app_version: include legacy appVersion string.
        :param fix_bools: apply js bool coercion for embedded runtime patches.
        :return: json-serializable ua dict.
        :rtype: dict
        """
        obj = {
            "userAgent": self.user_agent,
            "platform": self.platform,
            "acceptLanguage": self.language,
        }
        if self.metadata:
            # cdp metadata already exposes the proper camelCase structure
            obj["userAgentMetadata"] = self.metadata.to_json()
        if include_app_version:
            obj["appVersion"] = self.app_version
        if fix_bools:
            obj = self._fix_bools(obj)
        return obj