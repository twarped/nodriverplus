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

    // screen orientation
    if (!screen.orientation) {
        defineNativeGetter(screen, "orientation", {
            type: "landscape-primary",
            angle: 0,
            onchange: null
        });
    }

    // PDF plugin support
    const pdfPlugins = [
        {
            // Chrome PDF Plugin -> x-google-chrome-pdf
            name: "Chrome PDF Plugin",
            filename: "internal-pdf-viewer",
            description: "Portable Document Format",
            mimes: [
                {
                    type: "application/x-google-chrome-pdf",
                    suffixes: "pdf",
                    description: "Portable Document Format",
                },
            ],
        },
        {
            // Chrome PDF Viewer -> application/pdf
            name: "Chrome PDF Viewer",
            filename: "mhjfbmdgcfjbbpaeojofohoefgiehjai",
            description: "",
            mimes: [
                {
                    type: "application/pdf",
                    suffixes: "pdf",
                    description: "",
                },
            ],
        },
    ];

    // --- Build native-ish Plugin/MimeType objects ---
    function makeMimeType(mt, pluginRef) {
        const o = {};
        defineData(o, "type", mt.type);
        defineData(o, "suffixes", mt.suffixes);
        defineData(o, "description", mt.description || "");
        defineData(o, "enabledPlugin", pluginRef);
        return o;
    }

    function makePlugin(def) {
        const p = {};

        // fields = DATA props
        defineData(p, "name", def.name);
        defineData(p, "filename", def.filename);
        defineData(p, "description", def.description || "");

        const mimes = def.mimes.map(mt => makeMimeType(mt, p));

        // index slots = DATA, enumerable
        mimes.forEach((mt, i) => defineData(p, i, mt, { enumerable: true }));
        // named by type = DATA, non-enumerable
        mimes.forEach(mt => defineData(p, mt.type, mt));
        defineData(p, "length", mimes.length);

        return { plugin: p, mimes };
    }

    function makePluginArray(pluginObjs) {
        const arr = {};
        pluginObjs.forEach((plug, i) => defineData(arr, i, plug, { enumerable: true }));
        pluginObjs.forEach(plug => defineData(arr, plug.name, plug));
        defineData(arr, "length", pluginObjs.length);
        return arr;
    }

    function makeMimeTypeArray(allMimes) {
        const order = ["application/pdf", "application/x-google-chrome-pdf"];
        const ordered = allMimes.slice().sort((a, b) => order.indexOf(a.type) - order.indexOf(b.type));
        const arr = {};
        ordered.forEach((mt, i) => defineData(arr, i, mt, { enumerable: true }));
        ordered.forEach(mt => defineData(arr, mt.type, mt));
        defineData(arr, "length", ordered.length);
        return arr;
    }

    // Build
    const built = pdfPlugins.map(makePlugin);
    const pluginObjs = built.map(b => b.plugin);
    const allMimeObjs = built.flatMap(b => b.mimes);

    const pluginsArray = makePluginArray(pluginObjs);
    const mimeTypesArray = makeMimeTypeArray(allMimeObjs);

    const realPlugins = Object.getOwnPropertyDescriptor(Navigator.prototype, "plugins").get.call(navigator);
    const realPluginProto = realPlugins.length ? Object.getPrototypeOf(realPlugins[0]) : Object.prototype;
    const realMimeProto = realPlugins.length && realPlugins[0].length ? Object.getPrototypeOf(realPlugins[0][0]) : Object.prototype;
    const realArrayProto = Object.getPrototypeOf(realPlugins);

    Object.setPrototypeOf(pluginsArray, realArrayProto);
    pluginObjs.forEach(p => Object.setPrototypeOf(p, realPluginProto));
    allMimeObjs.forEach(m => Object.setPrototypeOf(m, realMimeProto));

    // Attach to navigator — these MUST be getters (that's how Chrome/Edge do it)
    Object.defineProperty(navProto, "plugins", {
        get: maskNative(function getPlugins() { return pluginsArray; }, "function get plugins() { [native code] }"),
        configurable: true
    });
    Object.defineProperty(navProto, "mimeTypes", {
        get: maskNative(function getMimeTypes() { return mimeTypesArray; }, "function get mimeTypes() { [native code] }"),
        configurable: true
    });
    Object.defineProperty(navigator, "pdfViewerEnabled", {
        get: maskNative(function getPdfViewerEnabled() { return true; }, "function get pdfViewerEnabled() { [native code] }"),
        configurable: true
    });
    defineData(navigator, "pdfViewerEnabled", true);

    // canvas fingerprint protection
    const getImageData = CanvasRenderingContext2D.prototype.getImageData;
    defineMethod(CanvasRenderingContext2D.prototype, "getImageData", function (x, y, w, h) {
        const imageData = getImageData.apply(this, arguments);

        // skip 1x1 transparent checks
        if (w <= 1 && h <= 1 && x === 0 && y === 0) {
            return imageData;
        }

        // add minimal noise
        const data = imageData.data;
        for (let i = 0; i < data.length; i += 4) {
            const noise = Math.random() < 0.1 ? (Math.random() < 0.5 ? -1 : 1) : 0;
            data[i] = Math.max(0, Math.min(255, data[i] + noise));
            data[i + 1] = Math.max(0, Math.min(255, data[i + 1] + noise));
            data[i + 2] = Math.max(0, Math.min(255, data[i + 2] + noise));
        }

        return imageData;
    });

    // cap error stack trace
    Error.stackTraceLimit = 10;
})();
