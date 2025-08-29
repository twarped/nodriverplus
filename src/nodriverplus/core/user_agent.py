"""user agent models + helpers.

wraps structured ua + metadata returned from a page script and provides
serialization helpers (including js-friendly bool coercion) for patching
cdp domains (Network / Emulation) and runtime navigator.* surfaces.
"""

class JSObject:
    """tiny helper mixin for js-friendly serialization tweaks."""
    def fix_bools_in_obj(self, obj):
        """recursively coerce python bools into lowercase js bool literals.

        used when embedding a python-built dict into a js string so we don't
        end up with capitalized True/False tokens in runtime patches.

        :param obj: arbitrary python structure (dict/list/scalar).
        :return: same structure with bools replaced by "true"/"false".
        """
        if isinstance(obj, bool):
            return "true" if obj else "false"
        elif isinstance(obj, dict):
            return {k: self.fix_bools_in_obj(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self.fix_bools_in_obj(v) for v in obj]
        else:
            return obj

class UserAgentMetadata(JSObject):
    """structured client hints / metadata describing platform + versions.

    mirrors chrome userAgentData surfaces (platform/version/brands/etc.).

    values are passed directly into Network / Emulation overrides and also
    injected into runtime patches so `navigator.userAgentData` matches. 
    (reduces fingerprint)
    """
    platform: str
    platform_version: str
    architecture: str
    model: str
    mobile: bool
    brands: list[dict[str, str]]
    full_version_list: list[dict[str, str]]
    full_version: str
    bitness: str
    wow64: bool
    form_factors: list[str]

    def __init__(self,
        platform: str,
        platform_version: str,
        architecture: str,
        model: str,
        mobile: bool,
        brands: list[dict[str, str]],
        full_version_list: list[dict[str, str]],
        full_version: str,
        bitness: str,
        wow64: bool,
        form_factors: list[str],
    ):
        self.platform = platform
        self.platform_version = platform_version
        self.architecture = architecture
        self.model = model
        self.mobile = mobile
        self.brands = brands
        self.full_version_list = full_version_list
        self.full_version = full_version
        self.bitness = bitness
        self.wow64 = wow64
        self.form_factors = form_factors

    def to_json(self, fix_bools = False) -> dict[str, str | bool | list[str] | list[dict[str, str]]]:
        """serialize to dict matching cdp expectations.

        :param fix_bools: when True convert python bools to js-friendly strings.
        :return: json-serializable metadata dict.
        :rtype: dict
        """
        obj = {
            "platform": self.platform,
            "platformVersion": self.platform_version,
            "architecture": self.architecture,
            "model": self.model,
            "mobile": self.mobile,
            "brands": self.brands,
            "fullVersionList": self.full_version_list,
            "fullVersion": self.full_version,
            "bitness": self.bitness,
            "wow64": self.wow64,
            "formFactors": self.form_factors,
        }
        if fix_bools:
            obj = self.fix_bools_in_obj(obj)
        return obj

class UserAgent(JSObject):
    """primary user agent container (raw UA string + platform + metadata).

    also exposes acceptLanguage and an appVersion if specified. 
    `app_version` defaults to `user_agent` (minus the "Mozilla/" prefix).

    used for both protocol overrides and runtime
    patch injection inside pages/workers.
    """
    user_agent: str
    platform: str
    language: str
    metadata: UserAgentMetadata | None
    app_version: str

    def __init__(self, 
        user_agent: str,
        platform: str,
        language: str,
        metadata: UserAgentMetadata = None,
        app_version: str = None
    ):
        self.user_agent = user_agent
        self.platform = platform
        self.language = language
        self.metadata = metadata
        self.app_version = app_version or user_agent.removeprefix("Mozilla/")

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
            obj["userAgentMetadata"] = self.metadata.to_json(fix_bools=fix_bools)
        if include_app_version:
            obj["appVersion"] = self.app_version
        if fix_bools:
            obj = self.fix_bools_in_obj(obj)
        return obj