from __future__ import annotations

TARGET_DOMAINS_RAW: dict[str, list[str] | str] = {
    # top-level (browser) target
    "browser": [
        "Browser", "Target", "IO", "SystemInfo",
        "Tethering", "Tracing", "Memory", "Log",
    ],

    # page-like targets
    "page": [
        "Page", "DOM", "CSS", "Runtime", "Network",
        "Performance", "Profiler", "Accessibility",
        "Overlay", "Emulation", "Log", "Security",
        "Animation", "Preload", "Fetch", "DOMDebugger",
        "DOMSnapshot", "LayerTree", "PerformanceTimeline",
        "Tracing", "Input", "Media", "Storage",
        "IndexedDB", "CacheStorage",
    ],
    "tab":             "page",
    "webview":         "page",
    "guest":           "page",
    "background_page": "page",
    "app":             "page",

    "other":           "page", 

    # frames / workers
    "iframe": [
        "DOM", "CSS", "Runtime", "Network",
        "Animation", "Performance", "Log",
        "DOMDebugger", "DOMSnapshot", "LayerTree",
        "Debugger",
    ],
    "dedicated_worker": [
        "Runtime", "Debugger", "Log", "Profiler",
    ],
    "worker": [
        "Runtime", "Debugger", "Log", "Profiler",
    ],
    "shared_worker": [
        "Runtime", "Debugger", "Log", "Profiler",
    ],
    "service_worker": [
        "Runtime", "Debugger", "Log", "Profiler", "ServiceWorker",
    ],
    "worklet": [
        "Runtime", "Log",
    ],
    "shared_storage_worklet": [
        "Runtime", "Log",
    ],
    "auction_worklet": [
        "Runtime", "Log",
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