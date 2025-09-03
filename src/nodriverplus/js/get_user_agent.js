(async () => {
    const ua = navigator.userAgent;
    const uaData = navigator.userAgentData;
    let metadata = null;
    if (uaData) {
        const highEntropy = await uaData.getHighEntropyValues([
            'architecture', 'bitness', 'formFactors', 'fullVersionList',
            'model', 'platformVersion', 'uaFullVersion', 'wow64'
        ]);
        metadata = {
            platform: uaData.platform || '',
            platformVersion: highEntropy.platformVersion || '',
            architecture: highEntropy.architecture || '',
            model: highEntropy.model || '',
            mobile: uaData.mobile || false,
            brands: uaData.brands || [],
            fullVersionList: highEntropy.fullVersionList || [],
            fullVersion: highEntropy.uaFullVersion || '',
            bitness: highEntropy.bitness || '',
            wow64: highEntropy.wow64 || false,
            formFactors: highEntropy.formFactors || [],
        };
    }
    return JSON.stringify({ 
        userAgent: ua, 
        platform: navigator.platform,
        acceptLanguage: navigator.language,
        metadata: metadata, 
    });
})()