/**
 * The one authority for the Tofu server address the extension talks to.
 * Entry: normalizeTofuServerUrl(input, {baseUrl}) — pure, lexical; resolveTofuServerUrl
 * (input, {fetch, timeoutMs}) — normalizes, then confirms the real base by
 * probing GET <candidate>/api/health on the address and each path ancestor.
 *
 * Users paste whatever their address bar shows: the SPA routes by hash
 * (…/proxy/15000/#/chat), the server serves the app at <base>/ and
 * <base>/index.html, and every API/asset lives under <base>/api/ or
 * <base>/static/. A reverse-proxy prefix (/proxy/15000) is part of the base
 * and must survive. A selected base disambiguates route-like proxy segments;
 * without that context /api/ and /static/ paths are refused, not truncated.
 * Every consumer — popup save, stored value, download
 * preseed, open-tab detection, repair-tab reaping — goes through here.
 * Loaded synchronously by the worker; no Chrome dependency.
 */

const TOFU_SERVER_ROUTE_ROOTS = ['/api/', '/static/'];

function _tofuDefaultScheme(hostPort) {
  const host = hostPort.replace(/:\d*$/, '').toLowerCase();
  const isLocal = host === 'localhost' || host.endsWith('.localhost')
    || /^\[[0-9a-f:.]+\]$/.test(host) || /^\d{1,3}(\.\d{1,3}){3}$/.test(host);
  // Loopback, LAN IPs and explicit ports are almost always plain-HTTP Tofu
  // servers; a bare hostname is a TLS gateway. resolve probes the other
  // scheme too, so this is only the first guess.
  return (isLocal || /:\d+$/.test(hostPort)) ? 'http' : 'https';
}

function normalizeTofuServerUrl(input, {baseUrl = ''} = {}) {
  let text = String(input == null ? '' : input).trim().replace(/^<(.*)>$/, '$1').trim();
  if (!text) {
    return {ok: false, error: 'Enter your Tofu server address, e.g. http://localhost:15000.'};
  }
  let schemeDefaulted = false;
  const scheme = /^([a-z][a-z0-9+.-]*):\/\//i.exec(text);
  if (!scheme) {
    if (/^[a-z][a-z0-9+.-]*:(?!\d)/i.test(text)) {
      return {ok: false, error: 'This is not a web address — use http:// or https://.'};
    }
    text = text.replace(/^\/\//, '');
    text = `${_tofuDefaultScheme(text.split(/[/?#]/)[0])}://${text}`;
    schemeDefaulted = true;
  } else if (!['http', 'https'].includes(scheme[1].toLowerCase())) {
    return {ok: false, error: 'Unsupported scheme — the Tofu server address must start with http:// or https://.'};
  }
  let parsed;
  try {
    parsed = new URL(text);
  } catch (_) {
    // Rejection messages also go to the worker log; never echo pasted input.
    return {ok: false, error: 'This is not a valid web address (expected something like https://host/proxy/15000).'};
  }
  if (!parsed.hostname) {
    return {ok: false, error: 'The address has no host name (expected something like https://host/proxy/15000).'};
  }
  if (parsed.username || parsed.password) {
    return {ok: false, error: 'Remove the "user:password@" part — the bridge authenticates with its bridge key, not URL credentials.'};
  }
  let path = parsed.pathname.replace(/\/index\.html?$/i, '').replace(/\/+$/, '');
  let selected = null;
  try { selected = new URL(baseUrl); } catch (_) {}
  if (selected && ['http:', 'https:'].includes(selected.protocol)
      && !selected.username && !selected.password && !selected.search && !selected.hash
      && selected.origin === parsed.origin) {
    const mount = selected.pathname.replace(/\/+$/, '');
    if (path === mount || TOFU_SERVER_ROUTE_ROOTS.some(root => (path + '/').startsWith(mount + root))) {
      path = mount;
    } else {
      selected = null;
    }
  } else {
    selected = null;
  }
  if (!selected && TOFU_SERVER_ROUTE_ROOTS.some(root => (path + '/').includes(root))) {
    return {ok: false, error: 'Cannot determine the Tofu base safely: /api/ or /static/ may be a proxy prefix or an application route. Restore the explicitly configured base, or use an unambiguous app address; no shortened address was saved.'};
  }
  const url = parsed.origin + path;
  return {ok: true, url, schemeDefaulted, changed: url !== String(input).trim()};
}

/* Bases worth probing, most specific first: the normalized address, then each
 * path ancestor (a pasted …/proxy/15000/some/page still finds /proxy/15000),
 * then — only when the user typed no scheme — the same list on the other one. */
function tofuServerUrlCandidates(normalized) {
  const parsed = new URL(normalized.url);
  // Do not rebuild path segments: interior slashes can be significant to
  // the proxy, even though trailing slashes are not part of a Tofu base.
  const paths = [];
  for (let path = parsed.pathname.replace(/\/+$/, ''); path; ) {
    paths.push(path);
    path = path.slice(0, path.lastIndexOf('/')).replace(/\/+$/, '');
  }
  paths.push('');
  const origins = [parsed.origin];
  if (normalized.schemeDefaulted) {
    const other = new URL(parsed.origin);
    other.protocol = parsed.protocol === 'https:' ? 'http:' : 'https:';
    origins.push(other.origin);
  }
  const out = [];
  for (const origin of origins) {
    for (const path of paths) out.push(origin + path);
  }
  return out;
}

async function _probeTofuBase(base, fetchPort, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetchPort(`${base}/api/health`, {
      method: 'GET', credentials: 'include', signal: controller.signal,
      headers: {'Accept': 'application/json'},
    });
    if (!response.ok) return {ok: false, reason: `HTTP ${response.status}`};
    const body = await response.json();
    return (body && body.ok === true && typeof body.version === 'string')
      ? {ok: true} : {ok: false, reason: 'not a Tofu server'};
  } catch (error) {
    return {ok: false, reason: controller.signal.aborted ? 'timed out'
      : (error && error.name === 'SyntaxError' ? 'not a Tofu server' : String(error && error.message || error))};
  } finally {
    clearTimeout(timer);
  }
}

async function resolveTofuServerUrl(input, {fetch: fetchPort, timeoutMs, baseUrl = ''}) {
  const normalized = normalizeTofuServerUrl(input, {baseUrl});
  if (!normalized.ok) return normalized;
  const selected = normalizeTofuServerUrl(baseUrl, {baseUrl});
  const candidates = selected.ok && selected.url === normalized.url
    ? [normalized.url] : tofuServerUrlCandidates(normalized);
  // Probe all at once: an unreachable host costs one deadline, not one per
  // candidate. The most specific confirmed base wins.
  const probes = await Promise.all(candidates.map(c => _probeTofuBase(c, fetchPort, timeoutMs)));
  const hit = probes.findIndex(p => p.ok);
  if (hit >= 0) {
    const url = candidates[hit];
    return {ok: true, url, verified: true, changed: url !== String(input).trim()};
  }
  return {...normalized, verified: false,
    warning: `No Tofu server answered at ${normalized.url}/api/health (${probes[0].reason}); saved anyway — the bridge keeps retrying.`};
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {normalizeTofuServerUrl, resolveTofuServerUrl, tofuServerUrlCandidates};
}
