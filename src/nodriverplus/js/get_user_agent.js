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
            platform: highEntropy.platform || '',
            platform_version: highEntropy.platformVersion || '',
            architecture: highEntropy.architecture || '',
            model: highEntropy.model || '',
            mobile: highEntropy.mobile || false,
            brands: highEntropy.brands || [],
            full_version_list: highEntropy.fullVersionList || [],
            full_version: highEntropy.uaFullVersion || '',
            bitness: highEntropy.bitness || '',
            wow64: highEntropy.wow64 || false,
            form_factors: highEntropy.formFactors || [],
        };
    }
    return JSON.stringify({ 
        user_agent: ua, 
        platform: navigator.platform,
        language: navigator.language,
        metadata: metadata, 
    });
})()