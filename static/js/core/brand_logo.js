/* ═══════════════════════════════════════════════════════════════════
   core/brand_logo.js — THE single source of the brand mascot URL

   Two problems this owns:

   1. CACHE-BUST. Every reference to the welcome mascot used to be a bare
      `static/icons/tofu-welcome.svg`, and the server serves icons with
      `cache-control: public, max-age=86400`. So any change to the file was
      invisible for up to 24h — a logo rollback looked like it "didn't
      happen". Role icons already solved this with a `?v=` token
      (orchestration-catalog.js `_ORCH_ICON_VER`); the main logo did not.
      `LOGO_VER` below is that token for the brand mascot. Bump it whenever
      the art changes.

   2. RUNTIME TRY-ON. Judging a logo from a contact sheet (or even a
      screenshot) has twice produced an approval that was reversed once the
      art was actually in the product. So a candidate is a SKIN the user can
      wear: switch it in Settings → General, keep working, and decide from
      lived experience. `default` is always the shipped original — a
      candidate never becomes the default by being added here.

   Fallback contract: an unknown / removed skin id resolves to `default`,
   and every <img> gets an onerror that swaps in the default URL. A missing
   candidate file can therefore never leave a blank logo.

   This file is concatenated by lib/js_bundler.py; symbols share the global
   window scope. No exports.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  /* Bump on every change to the mascot art (defeats the 24h icon cache). */
  var LOGO_VER = '20260729a';

  var STORAGE_KEY = 'tofu_logo_skin';
  var DEFAULT_SKIN = 'default';

  /* Skin registry. `default` MUST stay first and MUST point at the shipped
   * tofu-welcome.svg — the try-on mechanism is additive only. */
  var SKINS = [
    {
      id: 'default',
      label: 'settings.logoSkinDefault',
      path: '/static/icons/tofu-welcome.svg',
    },
    {
      id: 'a2-soft',
      label: 'settings.logoSkinA2',
      path: '/static/icons/_gen/logo-redesign/candidate-a2-soft.svg',
    },
    {
      /* Pixel-refined: keep the current mascot's pixel character and
       * hand-made feel; fix ONLY the VTracer artefacts (ragged staircases,
       * wobbling internal seams). Cube generated analytically, face laid out
       * by hand — see gen_pixel_refined.py. */
      id: 'pixel-refined',
      label: 'settings.logoSkinPixel',
      path: '/static/icons/_gen/logo-redesign/candidate-pixel-refined.svg',
    },
    {
      /* Minimal: the subtraction bet. Flat block, two eyes, one small smile,
       * no gradients / sheen / blush. Everything that only reads at 64px is
       * cut so the silhouette carries the identity at 16px. */
      id: 'minimal',
      label: 'settings.logoSkinMinimal',
      path: '/static/icons/_gen/logo-redesign/candidate-minimal.svg',
    },
    {
      /* Hand-drawn: warmth over precision. Every geometric redraw read as
       * "clean but cold", so this one puts the imperfection back on purpose —
       * uneven corners, varying stroke weight, an asymmetric face. */
      id: 'handdrawn',
      label: 'settings.logoSkinHandDrawn',
      path: '/static/icons/_gen/logo-redesign/candidate-handdrawn.svg',
    },
  ];

  function _base() {
    return (typeof BASE_PATH !== 'undefined' && BASE_PATH) ? BASE_PATH : '';
  }

  function _skinById(id) {
    for (var i = 0; i < SKINS.length; i++) {
      if (SKINS[i].id === id) return SKINS[i];
    }
    return null;
  }

  /** The default skin's URL — the fallback target for every failure path. */
  function defaultLogoUrl() {
    return _base() + SKINS[0].path + '?v=' + LOGO_VER;
  }

  /** Currently selected skin id. Unknown / absent → DEFAULT_SKIN. */
  function getLogoSkin() {
    var id;
    try {
      id = localStorage.getItem(STORAGE_KEY);
    } catch (e) {
      /* private mode / storage disabled — the default is the right answer */
      return DEFAULT_SKIN;
    }
    return _skinById(id) ? id : DEFAULT_SKIN;
  }

  /** Resolve the mascot URL for the active skin (always cache-busted). */
  function logoUrl() {
    var s = _skinById(getLogoSkin()) || SKINS[0];
    return _base() + s.path + '?v=' + LOGO_VER;
  }

  /** Repoint every live mascot <img> + the favicon at the active skin. */
  function applyLogoSkin() {
    var url = logoUrl();
    var imgs = document.querySelectorAll('img[data-brand-logo]');
    for (var i = 0; i < imgs.length; i++) {
      if (imgs[i].getAttribute('src') !== url) imgs[i].setAttribute('src', url);
    }
    var fav = document.querySelector('link[rel="icon"]');
    if (fav) fav.setAttribute('href', url);
  }

  /** Persist a skin choice and apply it immediately (no reload needed). */
  function setLogoSkin(id) {
    var chosen = _skinById(id) ? id : DEFAULT_SKIN;
    try {
      localStorage.setItem(STORAGE_KEY, chosen);
    } catch (e) {
      if (typeof debugLog === 'function') debugLog('[Logo] persist failed: ' + e, 'warning');
    }
    applyLogoSkin();
    document.querySelectorAll('.logo-skin-option').forEach(function (el) {
      el.classList.toggle('active', el.dataset.skin === chosen);
    });
    return chosen;
  }

  /* The onerror handler for mascot <img> tags: a candidate file that 404s
   * falls back to the shipped default instead of rendering nothing. Guarded
   * so a broken default can't loop. */
  function onBrandLogoError(img) {
    if (!img || img.dataset.brandLogoFellBack === '1') return;
    img.dataset.brandLogoFellBack = '1';
    img.setAttribute('src', defaultLogoUrl());
    if (typeof debugLog === 'function') {
      debugLog('[Logo] skin asset failed to load — fell back to default', 'warning');
    }
  }

  /** The <img> attribute soup every render site needs, as one string.
   *  Callers splice this into their template so the marker attribute, the
   *  cache-busted URL and the fallback handler can never drift apart. */
  function brandLogoImgAttrs(size) {
    var s = size || 64;
    return 'src="' + logoUrl() + '" data-brand-logo="1" alt="Tofu" '
      + 'width="' + s + '" height="' + s + '" '
      + 'onerror="onBrandLogoError(this)"';
  }

  function listLogoSkins() {
    return SKINS.map(function (s) { return { id: s.id, label: s.label, path: s.path }; });
  }

  window.LOGO_VER = LOGO_VER;
  window.getLogoSkin = getLogoSkin;
  window.setLogoSkin = setLogoSkin;
  window.logoUrl = logoUrl;
  window.defaultLogoUrl = defaultLogoUrl;
  window.applyLogoSkin = applyLogoSkin;
  window.onBrandLogoError = onBrandLogoError;
  window.brandLogoImgAttrs = brandLogoImgAttrs;
  window.listLogoSkins = listLogoSkins;
})();
