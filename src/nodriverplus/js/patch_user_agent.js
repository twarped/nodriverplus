(() => {
    // --- 1) One-time toString masker ---
    const _fnToString = Function.prototype.toString;
    const _mask = new WeakMap();

    Function.prototype.toString = new Proxy(_fnToString, {
        apply(target, thisArg, args) {
            if (_mask.has(thisArg)) return _mask.get(thisArg);
            return Reflect.apply(target, thisArg, args);
        }
    });

    function maskNative(fn, src) {
        // src e.g. 'function item() { [native code] }' or 'function get plugins() { [native code] }'
        _mask.set(fn, src);
        try { Object.defineProperty(fn, "length", { value: fn.length, configurable: true }); } catch { }
        return fn;
    }

    function defineNativeGetter(obj, prop, val, { enumerable = false } = {}) {
        const getter = function () { return val; };
        Object.defineProperty(obj, prop, {
            get: maskNative(getter, `function get ${prop}() { [native code] }`),
            configurable: true,
            enumerable,
        });
    }

    /* example placeholder replacement:
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
            'mobile': False, 
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
            'wow64': False, 
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