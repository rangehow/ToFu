// Immersive Translate 1.34.x: put a fresh test profile in the state of a
// long-time user. A new install shows the float-ball guide ("click to
// translate") once per profile, with a looping Lottie arrow that runs a
// requestAnimationFrame loop and invalidates style/layout every frame until it
// is dismissed. Marking the guide as shown and dismissed (the keys the content
// script reads through chrome.storage.sync) keeps that first-run animation out
// of soaks that model the owner's browser.
// Evaluated in the extension service worker by soak.py --extension-eval.
(async () => {
  const prefix = 'immersiveTranslateSyncStoreKey_';
  await chrome.storage.sync.set({
    [prefix + 'floatBallGuideShowCount']: 1,
    [prefix + 'floatBallGuideDismissedAt']: new Date().toISOString(),
  });
  const check = await chrome.storage.sync.get([prefix + 'floatBallGuideShowCount', prefix + 'floatBallGuideDismissedAt']);
  return JSON.stringify(check);
})()
