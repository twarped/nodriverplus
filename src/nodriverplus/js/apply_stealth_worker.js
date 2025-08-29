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

    // --- 2) Define helpers ---
    function defineData(obj, prop, value, { enumerable = false, writable = false } = {}) {
        Object.defineProperty(obj, prop, { value, enumerable, writable, configurable: true });
    }

    function defineMethod(obj, prop, fn, { enumerable = false } = {}) {
        defineData(obj, prop, maskNative(fn, `function ${prop}() { [native code] }`), { enumerable });
    }

    function defineIterator(obj, generatorFn, name = "values") {
        defineData(obj, Symbol.iterator, maskNative(generatorFn, `function ${name}() { [native code] }`));
    }

    function defineNativeGetter(obj, prop, val, { enumerable = false } = {}) {
        const getter = function () { return val; };
        Object.defineProperty(obj, prop, {
            get: maskNative(getter, `function get ${prop}() { [native code] }`),
            configurable: true,
            enumerable,
        });
    }

    const navProto = Object.getPrototypeOf(navigator);

    // hide webdriver flag
    defineNativeGetter(navProto, "webdriver", false);

    // spoof languages
    defineNativeGetter(navProto, "language", "en-US");
    defineNativeGetter(navProto, "languages", ["en-US", "en"]);
})();