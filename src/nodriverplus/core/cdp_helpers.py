from __future__ import annotations


# probably pretty accurate as of chrome 140+, but this was generated
# by GPT-5 Thinking Deep-Research rather than me creating this from
# chromium source code... but it seems to work just fine.
TARGET_DOMAINS_RAW: dict[str, list[str] | str] = {
    # --- Browser (top-level) target ---
    "browser": [
        # Browser/global control & discovery
        "Browser", "Target", "SystemInfo", "Tethering", "IO",
        # Tracing & memory instrumentation that can run at browser level
        "Tracing", "Memory",
        # Misc/admin-ish domains commonly usable from the browser session
        "Cast", "HeadlessExperimental", "Log",
    ],

    # --- Page-like targets (tabs, extension pages, webviews, etc.) ---
    "page": [
        # Core page+renderer control
        "Page", "Runtime", "Debugger", "Console", "Log",
        # DOM/CSS stack
        "DOM", "CSS", "DOMDebugger", "DOMSnapshot", "DOMStorage",
        # Networking & request interception (page/renderer scope)
        "Network", "Fetch",
        # Performance / timeline / profiling
        "Performance", "PerformanceTimeline", "Profiler", "HeapProfiler", "Tracing", "Memory",
        # Rendering / visuals
        "Animation", "Overlay", "LayerTree",
        # Emulation & input
        "Emulation", "Input", "DeviceOrientation",
        # Security / storage
        "Security", "Storage", "IndexedDB", "CacheStorage",
        # Media & platform features
        "Media", "WebAudio", "WebAuthn", "BluetoothEmulation", "DeviceAccess",
        # Loading & preload hints
        "Preload",
        # UX and A11y
        "Accessibility",
        # Identity / permissions flows
        "FedCm",
        # Extensions & PWA-related hooks (exposed for extension/app targets)
        "Extensions", "PWA",
        # Cast control from page context (supported in Chromium)
        "Cast",
        # Misc protocol plumbing
        "Inspector", "Target", "IO",
        # Audits (protocol surface exists; availability may vary by channel)
        "Audits",
    ],
    "tab":             "page",
    "webview":         "page",
    "guest":           "page",
    "background_page": "page",
    "app":             "page",
    "other":           "page",

    # --- Frames / iframes ---
    # Note: subframes do NOT expose Page.*; Network events roll up to the tab/page.
    "iframe": [
        "Runtime", "Debugger", "Console", "Log",
        "DOM", "CSS", "DOMDebugger", "DOMSnapshot",
        "LayerTree",
        "Animation",
        "Performance", "PerformanceTimeline",
        # Keep this renderer-scoped; omit Page/Network/Input here.
    ],

    # --- Workers ---
    # Dedicated worker (CDP type "worker"); shared & service workers below.
    "worker": [
        "Runtime", "Debugger", "Console", "Log",
        "Profiler", "HeapProfiler",
        # Intentionally exclude Network/Fetch here; Chromium does not expose
        # Fetch.enable to worker sessions.
    ],
    "dedicated_worker": "worker",
    "shared_worker":    "worker",

    # --- Service workers (adds ServiceWorker domain) ---
    "service_worker": [
        "Runtime", "Debugger", "Console", "Log",
        "Profiler", "HeapProfiler",
        "ServiceWorker",
    ],

    # --- Worklets ---
    # Worklets are very restricted: execution + logs; debugger support varies by channel.
    "worklet": [
        "Runtime", "Console", "Log",
    ],
    "shared_storage_worklet": [
        "Runtime", "Console", "Log",
    ],
    "auction_worklet": [
        "Runtime", "Console", "Log",
    ],
}

def _resolve_aliases(
    mapping: dict[str, list[str] | str]
) -> dict[str, list[str]]:
    """replace string aliases with concrete lists so callers always get list[str]."""
    resolved: dict[str, list[str]] = {}
    for k, v in mapping.items():
        if isinstance(v, str):
            ref = mapping.get(v)
            if not isinstance(ref, list):
                raise KeyError(f"alias {k!r} points to unknown/non-list target {v!r}")
            resolved[k] = list(ref)
        else:
            resolved[k] = list(v)
    return resolved


TARGET_DOMAINS: dict[str, list[str]] = _resolve_aliases(TARGET_DOMAINS_RAW)

    
def _resolve(tt: str) -> str:
    """
    map alias -> canonical target_type (page/background_page/etc. -> page).

    if `tt` is already canonical, it comes back untouched.
    """
    val = TARGET_DOMAINS.get(tt)
    return _resolve(val) if isinstance(val, str) else tt


def domains_for(target_type: str) -> list[str]:
    """return the list of domains a target_type understands (empty if unknown)."""
    return TARGET_DOMAINS.get(_resolve(target_type), [])


def can_use_domain(target_type: str, domain: str) -> bool:
    """fast bool: does target_type expose the domain?"""
    return domain in domains_for(target_type)


def target_types_for(domain: str) -> list[str]:
    """reverse lookup: which target_types support `domain`?"""
    dom = domain.strip()
    return [
        tt
        for tt, doms in TARGET_DOMAINS.items()
        if not isinstance(doms, str) and dom in doms
    ]


def assert_domain(target_type: str, domain: str) -> None:
    """raise ValueError if `domain` isn't available on `target_type`."""
    if not can_use_domain(target_type, domain):
        raise ValueError(f"{target_type!r} cannot call {domain}.enable()")
    
__all__ = [
    "TARGET_DOMAINS",
    "domains_for",
    "can_use_domain",
    "target_types_for",
    "assert_domain",
]