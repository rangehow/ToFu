#!/usr/bin/env node
/* Responsibility: put a read-only TLS/HTTP2 front door before a Tofu server so a
   soak browser can watch production without writing to it.
   Entry: `node readonly_proxy.mjs --listen 127.0.0.1:18443 --upstream http://127.0.0.1:15000`.
   Dependencies: Node built-ins and the `openssl` CLI (self-signed certificate).

   Mirrors the owner's path (HTTPS, HTTP/2, `/proxy/<port>/` prefix stripped like
   the VS Code port proxy). Only GET/HEAD/OPTIONS are forwarded; every other
   method is answered locally with 405 and logged, never forwarded. On the
   /api/push WebSocket only subscribe/unsubscribe/ping frames and allow-listed
   read-only JSON-RPC methods are forwarded; anything else (e.g. `abort`) is
   dropped and logged. permessage-deflate is not offered upstream so every
   client frame stays inspectable.

   --frontend-dir DIR overlays another build's static/vite on the same live
   server: its files are served locally, the index HTML's entry scripts are
   rewritten to DIR's manifest entries, and buildId is stripped from push
   pongs so the page does not react to the server's (different) build. Two
   proxies, one with and one without the overlay, give a same-time A/B of a
   frontend change on the same live stream.

   --frontend-upstream URL is the overlay for a change that also edits the
   server-composed page (index.html, application-shell fragments): the page and
   every other non-API path come from URL, a scratch server running the
   candidate checkout, while /api reads and the push socket stay on the live
   upstream under the same read-only rules; buildId is stripped as above. */
import fs from 'node:fs';
import http from 'node:http';
import http2 from 'node:http2';
import net from 'node:net';
import path from 'node:path';
import { execFileSync } from 'node:child_process';

const args = Object.fromEntries(process.argv.slice(2).reduce((pairs, value, index, all) => {
  if (value.startsWith('--')) pairs.push([value.slice(2), all[index + 1]]);
  return pairs;
}, []));
const [listenHost, listenPort] = String(args.listen || '127.0.0.1:18443').split(':');
const upstream = new URL(args.upstream || 'http://127.0.0.1:15000');
const stripPrefix = String(args['strip-prefix'] || '/proxy/15000').replace(/\/$/, '');
const stateDir = path.resolve(args['state-dir'] || '.');
fs.mkdirSync(stateDir, { recursive: true });

const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);
const WS_ACTIONS = new Set(['subscribe', 'unsubscribe', 'ping']);
// lib/control_rpc.py METHODS is an explicit read allowlist; mirror it exactly.
const WS_RPC_METHODS = new Set(['project.browse', '$/cancelRequest']);
const HOP_BY_HOP = new Set(['connection', 'keep-alive', 'proxy-connection', 'transfer-encoding',
  'upgrade', 'te', 'trailer', 'http2-settings', 'host']);

const keyPath = path.join(stateDir, 'proxy-key.pem');
const certPath = path.join(stateDir, 'proxy-cert.pem');
if (!fs.existsSync(keyPath) || !fs.existsSync(certPath)) {
  execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', keyPath,
    '-out', certPath, '-days', '30', '-subj', `/CN=${listenHost}`,
    '-addext', `subjectAltName=IP:${listenHost}`], { stdio: 'ignore' });
}

const frontendDir = args['frontend-dir'] ? path.resolve(args['frontend-dir']) : null;
const frontendUpstream = args['frontend-upstream'] ? new URL(args['frontend-upstream']) : null;
if (frontendDir && frontendUpstream) throw new Error('--frontend-dir and --frontend-upstream are alternatives');
const entryFiles = {};
if (frontendDir) {
  const manifest = JSON.parse(fs.readFileSync(path.join(frontendDir, 'manifest.json'), 'utf8'));
  for (const entry of Object.values(manifest)) {
    if (entry.isEntry) entryFiles[path.basename(entry.file).replace(/-[\w-]+\.js$/, '')] = entry.file;
  }
}
const CONTENT_TYPES = { '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.json': 'application/json', '.wasm': 'application/wasm',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2', '.woff': 'font/woff' };

const stats = { forwarded: 0, blocked: 0, wsOpened: 0, wsClientFrames: 0, wsDropped: 0,
  upstreamErrors: 0, overlayFiles: 0, overlayDocuments: 0, frontendForwarded: 0, buildIdsStripped: 0,
  byBlockedPath: {} };
const blockedLog = fs.createWriteStream(path.join(stateDir, 'proxy-blocked.jsonl'), { flags: 'a' });
const statsPath = path.join(stateDir, 'proxy-stats.json');
const redactPath = (value) => String(value || '').split('?')[0]
  .replace(/\/(coordinator|worker|mw)[-0-9a-z]+/g, '/<conv>')
  .replace(/\/[0-9a-f]{8}-[0-9a-f-]{27,}/g, '/<uuid>');
function recordBlocked(kind, detail) {
  stats.blocked += kind === 'http' ? 1 : 0;
  const key = `${kind} ${detail}`;
  stats.byBlockedPath[key] = (stats.byBlockedPath[key] || 0) + 1;
  blockedLog.write(JSON.stringify({ t: Date.now(), kind, detail }) + '\n');
}
setInterval(() => fs.writeFileSync(statsPath, JSON.stringify(stats, null, 1)), 10_000).unref();

function upstreamPath(rawPath) {
  const value = String(rawPath || '/');
  if (stripPrefix && (value === stripPrefix || value.startsWith(stripPrefix + '/'))) {
    return value.slice(stripPrefix.length) || '/';
  }
  return value;
}

// The browser's own authority is kept (as a transparent port proxy does): the
// server's cross-site guard compares Origin with Host for the push socket.
function forwardHeaders(source) {
  const headers = {};
  for (const [name, value] of Object.entries(source)) {
    if (name.startsWith(':') || HOP_BY_HOP.has(name.toLowerCase())) continue;
    headers[name] = value;
  }
  headers.host = source.host || source[':authority'] || upstream.host;
  return headers;
}

const agent = new http.Agent({ keepAlive: true, maxSockets: Infinity });

function serveOverlayFile(target, res) {
  const match = frontendDir && /^\/static\/vite\/([^?#]+)/.exec(target);
  if (!match) return false;
  const file = path.resolve(frontendDir, decodeURIComponent(match[1]));
  if (!file.startsWith(frontendDir + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return false;
  stats.overlayFiles += 1;
  res.writeHead(200, { 'content-type': CONTENT_TYPES[path.extname(file)] || 'application/octet-stream',
    'cache-control': 'no-store' });
  fs.createReadStream(file).pipe(res);
  return true;
}

function rewriteDocument(html) {
  let body = html;
  for (const [stem, file] of Object.entries(entryFiles)) {
    body = body.replace(new RegExp(`static/vite/assets/${stem}-[\\w-]+\\.js`, 'g'), `static/vite/${file}`);
  }
  return body;
}

function onRequest(req, res) {
  if (!SAFE_METHODS.has(req.method)) {
    recordBlocked('http', `${req.method} ${redactPath(upstreamPath(req.url))}`);
    res.writeHead(405, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ ok: false, error: { code: 'read_only_soak',
      message: 'Read-only soak proxy: write requests are not forwarded.' } }));
    req.resume();
    return;
  }
  const target = upstreamPath(req.url);
  if (req.method === 'GET' && serveOverlayFile(target, res)) return;
  const isDocument = Boolean(frontendDir) && req.method === 'GET' && target.split('?')[0] === '/';
  const headers = forwardHeaders(req.headers);
  if (isDocument) delete headers['accept-encoding'];
  const origin = frontendUpstream && !/^\/api(\/|\?|$)/.test(target) ? frontendUpstream : upstream;
  stats.forwarded += 1;
  if (origin === frontendUpstream) stats.frontendForwarded += 1;
  const outbound = http.request({ host: origin.hostname, port: origin.port, method: req.method,
    path: target, headers, agent }, (response) => {
    const headers = {};
    for (const [name, value] of Object.entries(response.headers)) {
      if (!HOP_BY_HOP.has(name.toLowerCase())) headers[name] = value;
    }
    if (isDocument && String(response.headers['content-type'] || '').includes('text/html')) {
      const chunks = [];
      response.on('data', (chunk) => chunks.push(chunk));
      response.on('end', () => {
        const body = Buffer.from(rewriteDocument(Buffer.concat(chunks).toString('utf8')));
        delete headers['content-length'];
        delete headers['content-encoding'];
        stats.overlayDocuments += 1;
        res.writeHead(response.statusCode || 502, { ...headers, 'content-length': body.length });
        res.end(body);
      });
      response.on('error', () => res.destroy());
      return;
    }
    res.writeHead(response.statusCode || 502, headers);
    response.pipe(res);
    response.on('error', () => res.destroy());
  });
  outbound.on('error', () => {
    stats.upstreamErrors += 1;
    if (!res.headersSent) { res.writeHead(502); res.end(); } else res.destroy();
  });
  res.on('close', () => outbound.destroy());
  req.pipe(outbound);
}

function allowedClientText(text) {
  let frame;
  try { frame = JSON.parse(text); } catch { return false; }
  if (!frame || typeof frame !== 'object') return false;
  if (frame.jsonrpc === '2.0') return WS_RPC_METHODS.has(frame.method);
  return WS_ACTIONS.has(frame.action);
}

/* Client frames are masked (RFC 6455 5.3). Forward a frame byte-for-byte only
   after its unmasked text passes the allowlist; control and binary frames pass. */
function clientFrameFilter(client, server) {
  let buffer = Buffer.alloc(0);
  client.on('data', (chunk) => {
    buffer = Buffer.concat([buffer, chunk]);
    for (;;) {
      if (buffer.length < 2) return;
      const fin = (buffer[0] & 0x80) !== 0;
      const opcode = buffer[0] & 0x0f;
      const masked = (buffer[1] & 0x80) !== 0;
      let length = buffer[1] & 0x7f;
      let offset = 2;
      if (length === 126) { if (buffer.length < 4) return; length = buffer.readUInt16BE(2); offset = 4; }
      else if (length === 127) {
        if (buffer.length < 10) return;
        length = Number(buffer.readBigUInt64BE(2)); offset = 10;
      }
      const maskOffset = offset;
      if (masked) offset += 4;
      if (buffer.length < offset + length) return;
      const frame = buffer.subarray(0, offset + length);
      buffer = buffer.subarray(offset + length);
      stats.wsClientFrames += 1;
      if (opcode === 0x1 || opcode === 0x0) {
        let allowed = false;
        if (opcode === 0x1 && fin && masked) {
          const payload = Buffer.from(frame.subarray(offset));
          for (let i = 0; i < payload.length; i++) payload[i] ^= frame[maskOffset + (i % 4)];
          allowed = allowedClientText(payload.toString('utf8'));
          if (!allowed) {
            let action = 'unparsed';
            try { const parsed = JSON.parse(payload.toString('utf8')); action = parsed.action || parsed.method || 'unknown'; } catch { /* keep */ }
            recordBlocked('ws', String(action).slice(0, 60));
          }
        } else {
          recordBlocked('ws', 'fragmented-text');
        }
        if (!allowed) { stats.wsDropped += 1; continue; }
      }
      server.write(frame);
    }
  });
}

function encodeTextFrame(text) {
  const payload = Buffer.from(text, 'utf8');
  let header;
  if (payload.length < 126) header = Buffer.from([0x81, payload.length]);
  else if (payload.length < 65536) { header = Buffer.alloc(4); header[0] = 0x81; header[1] = 126; header.writeUInt16BE(payload.length, 2); }
  else { header = Buffer.alloc(10); header[0] = 0x81; header[1] = 127; header.writeBigUInt64BE(BigInt(payload.length), 2); }
  return Buffer.concat([header, payload]);
}

/* Server frames are unmasked. After the 101 handshake passes through, rewrite
   only complete text frames that carry a buildId (push pongs). */
function serverBuildIdFilter(server, client) {
  let handshake = true;
  let buffer = Buffer.alloc(0);
  server.on('data', (chunk) => {
    buffer = Buffer.concat([buffer, chunk]);
    if (handshake) {
      const end = buffer.indexOf('\r\n\r\n');
      if (end < 0) return;
      client.write(buffer.subarray(0, end + 4));
      buffer = buffer.subarray(end + 4);
      handshake = false;
    }
    for (;;) {
      if (buffer.length < 2) return;
      const opcode = buffer[0] & 0x0f;
      let length = buffer[1] & 0x7f;
      let offset = 2;
      if (length === 126) { if (buffer.length < 4) return; length = buffer.readUInt16BE(2); offset = 4; }
      else if (length === 127) { if (buffer.length < 10) return; length = Number(buffer.readBigUInt64BE(2)); offset = 10; }
      if (buffer.length < offset + length) return;
      let frame = buffer.subarray(0, offset + length);
      buffer = buffer.subarray(offset + length);
      if (opcode === 0x1 && (frame[0] & 0x80) && !(frame[1] & 0x80)) {
        const text = frame.subarray(offset).toString('utf8');
        if (text.includes('"buildId"')) {
          try {
            const parsed = JSON.parse(text);
            delete parsed.buildId;
            frame = encodeTextFrame(JSON.stringify(parsed));
            stats.buildIdsStripped += 1;
          } catch { /* forward unchanged */ }
        }
      }
      client.write(frame);
    }
  });
}

function onUpgrade(req, socket, head) {
  const target = upstreamPath(req.url);
  if (req.method !== 'GET' || !/^\/api\/push(\?|$)/.test(target)) {
    recordBlocked('upgrade', redactPath(target));
    socket.destroy();
    return;
  }
  const server = net.connect(Number(upstream.port), upstream.hostname, () => {
    stats.wsOpened += 1;
    const lines = [`GET ${target} HTTP/1.1`];
    for (let i = 0; i < req.rawHeaders.length; i += 2) {
      const name = req.rawHeaders[i];
      if (/^sec-websocket-extensions$/i.test(name)) continue;
      lines.push(`${name}: ${req.rawHeaders[i + 1]}`);
    }
    server.write(lines.join('\r\n') + '\r\n\r\n');
    if (head && head.length) socket.unshift(head);
    if (frontendDir || frontendUpstream) serverBuildIdFilter(server, socket);
    else server.pipe(socket);
    clientFrameFilter(socket, server);
  });
  const close = () => { socket.destroy(); server.destroy(); };
  server.on('error', close); socket.on('error', close);
  server.on('close', close); socket.on('close', close);
}

const tlsServer = http2.createSecureServer({ key: fs.readFileSync(keyPath), cert: fs.readFileSync(certPath),
  allowHTTP1: true, settings: { maxConcurrentStreams: 1000 } }, onRequest);
tlsServer.on('upgrade', onUpgrade);
tlsServer.listen(Number(listenPort), listenHost, () => {
  process.stderr.write(`[readonly-proxy] https://${listenHost}:${listenPort}${stripPrefix}/ -> ${upstream.href}\n`);
});
