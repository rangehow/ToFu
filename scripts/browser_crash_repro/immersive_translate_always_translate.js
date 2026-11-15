// Immersive Translate 1.34.x: make the extension translate the soak page.
// Adds the soak origin to "always translate these sites"
// (translationUrlPattern.matches in chrome.storage.local fullLocalUserConfig,
// the setting the popup's "Always translate" writes) and sets the target
// language to English. With a zh-CN target the Chinese Tofu UI is detected as
// already in the target language and nothing is translated, so this exercises
// page-translation DOM work on a streaming page, not the owner's language pair.
// Evaluated in the extension service worker by soak.py --extension-eval.
(async () => {
  const key = 'fullLocalUserConfig';
  const stored = (await chrome.storage.local.get(key))[key] || {};
  stored.targetLanguage = 'en';
  stored.translationUrlPattern = {
    matches: ['127.0.0.1', '127.0.0.1:18443', 'https://127.0.0.1:18443/*'],
    excludeMatches: [],
  };
  await chrome.storage.local.set({ [key]: stored });
  const check = (await chrome.storage.local.get(key))[key];
  return JSON.stringify({ keys: Object.keys(check).length, targetLanguage: check.targetLanguage,
    matches: check.translationUrlPattern.matches });
})()
