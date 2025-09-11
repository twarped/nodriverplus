(() => {
    // assumes single application per global; no global guards or assignments
    const _originalFnToString = Function.prototype.toString;
    const _mask = new WeakMap();

    const _fnToStringProxy = new Proxy(_originalFnToString, {
        apply(target, thisArg, args) {
            if (_mask.has(thisArg)) return _mask.get(thisArg);
            return Reflect.apply(target, thisArg, args);
        }
    });
    try { Function.prototype.toString = _fnToStringProxy; } catch (e) { /* if locked, continue */ }
    // mask the proxy itself to prevent introspection
    maskNative(_fnToStringProxy, 'function toString() { [native code] }');

    // define shared native functions to avoid infinite recursion
    const _nativeToString = function () { return 'function toString() { [native code] }'; };
    maskNative(_nativeToString, 'function toString() { [native code] }');
    const _nativeToLocaleString = function () { return 'function toLocaleString() { [native code] }'; };
    maskNative(_nativeToLocaleString, 'function toLocaleString() { [native code] }');

    function maskNative(fn, src) {
        // src e.g. 'function item() { [native code] }' or 'function get plugins() { [native code] }'
        if (_mask.has(fn)) return fn; // avoid re-masking
        _mask.set(fn, src);
        try { Object.defineProperty(fn, "length", { value: fn.length, configurable: true }); } catch { }
        // recursively mask toString properties
        try {
            Object.defineProperty(fn, 'toString', {
                value: _nativeToString,
                configurable: true,
                writable: true,
                enumerable: false
            });
            // also mask toLocaleString
            Object.defineProperty(fn, 'toLocaleString', {
                value: _nativeToLocaleString,
                configurable: true,
                writable: true,
                enumerable: false
            });
        } catch {}
        return fn;
    }

    function defineNativeGetter(obj, prop, val, { enumerable = false } = {}) {
        // avoid redefining the getter if already defined to our masked getter
        const existing = Object.getOwnPropertyDescriptor(obj, prop);
        if (existing && typeof existing.get === 'function' && _mask.has(existing.get)) return;

        const getter = function () { return val; };
        Object.defineProperty(obj, prop, {
            get: maskNative(getter, `function get ${prop}() { [native code] }`),
            configurable: true,
            enumerable,
        });
    }

    /* example //uaPatch// placeholder replacement:
    const uaPatch = {
        'userAgent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36',
        'appVersion': '5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36',
        'platform': 'Win32',
        'acceptLanguage': 'en-US',
        // userAgentData if available (MacOS doesn't support this)
        'userAgentMetadata': { 
            'platform': 'Windows', 
            'platformVersion': '19.0.0', 
            'architecture': 'x86', 
            'model': '', 
            'mobile': false, 
            'brands': [
                { 
                    'brand': 'Not;A=Brand', 'version': '99' 
                }, { 
                    'brand': 'Google Chrome', 'version': '139' 
                }, { 
                    'brand': 'Chromium', 'version': '139' 
                }
            ], 
            'fullVersionList': [
                { 
                    'brand': 'Not;A=Brand', 'version': '99.0.0.0' 
                }, { 
                    'brand': 'Google Chrome', 'version': '139.0.7258.139' 
                }, { 
                    'brand': 'Chromium', 'version': '139.0.7258.139' 
                }
            ], 
            'fullVersion': '139.0.7258.139', 
            'bitness': '64', 
            'wow64': false, 
            'formFactors': [
                'Desktop'
            ] 
        }
    }
    */

    //uaPatch//

    const navProto = Object.getPrototypeOf(navigator);

    defineNativeGetter(navProto, "userAgent", uaPatch.userAgent);
    defineNativeGetter(navProto, "appVersion", uaPatch.appVersion);
    defineNativeGetter(navProto, "platform", uaPatch.platform);

    defineNativeGetter(navProto, "language", uaPatch.acceptLanguage);
    defineNativeGetter(navProto, "languages", [...uaPatch.acceptLanguage.split(",")]);
})();