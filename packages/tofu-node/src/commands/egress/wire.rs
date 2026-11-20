//! Minimal HTTP/1.1 client for the egress executor and the adapter download.
//!
//! One request per connection, direct or through an HTTP proxy (CONNECT for
//! https targets). The caller sees the raw TCP socket as soon as it connects,
//! so another thread can shut it down to abort a blocked read (the in-flight
//! cancel of `egress_http_stream`). Bodies are de-chunked and gzip/deflate
//! decoded like Requests' `iter_content`. Redirects are never followed here.

use std::io::{self, BufRead, BufReader, Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::time::Duration;

/// Python `http.client` limits on one header line and the header count.
const MAX_HEADER_LINE: usize = 65536;
const MAX_HEADERS: usize = 100;

/// A parsed URL with Python `urlparse` host semantics (userinfo stripped,
/// host lowercased, brackets removed from IPv6 literals).
#[derive(Debug, Clone, PartialEq)]
pub struct Url {
    pub scheme: String,
    pub host: String,
    pub explicit_port: Option<u16>,
    /// Origin-form request target (`/path?query`), fragment removed.
    pub target: String,
    userinfo: Option<String>,
}

impl Url {
    pub fn port(&self) -> u16 {
        self.explicit_port
            .unwrap_or(if self.scheme == "https" { 443 } else { 80 })
    }

    fn authority(&self) -> String {
        let host = if self.host.contains(':') {
            format!("[{}]", self.host)
        } else {
            self.host.clone()
        };
        match self.explicit_port {
            Some(port) if port != self.port_default() => format!("{host}:{port}"),
            _ => host,
        }
    }

    fn port_default(&self) -> u16 {
        if self.scheme == "https" {
            443
        } else {
            80
        }
    }

    fn host_port(&self) -> String {
        if self.host.contains(':') {
            format!("[{}]:{}", self.host, self.port())
        } else {
            format!("{}:{}", self.host, self.port())
        }
    }

    pub fn absolute(&self) -> String {
        format!("{}://{}{}", self.scheme, self.authority(), self.target)
    }

    /// Resolve a redirect `Location` against this URL.
    pub fn join(&self, location: &str) -> Result<Url, String> {
        if location.contains("://") {
            return parse_url(location);
        }
        if let Some(rest) = location.strip_prefix("//") {
            return parse_url(&format!("{}://{rest}", self.scheme));
        }
        let path = if location.starts_with('/') {
            location.to_string()
        } else {
            let base = self.target.split('?').next().unwrap_or("/");
            let dir = &base[..base.rfind('/').map(|i| i + 1).unwrap_or(0)];
            format!("{dir}{location}")
        };
        parse_url(&format!("{}://{}{path}", self.scheme, self.authority()))
    }
}

pub fn parse_url(url: &str) -> Result<Url, String> {
    let (scheme, rest) = url
        .split_once("://")
        .ok_or_else(|| format!("invalid URL {url:?}: no scheme"))?;
    if scheme.is_empty()
        || !scheme
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || "+-.".contains(c))
    {
        return Err(format!("invalid URL {url:?}: bad scheme"));
    }
    let end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let (authority, tail) = rest.split_at(end);
    let (userinfo, hostport) = match authority.rsplit_once('@') {
        Some((user, host)) => (Some(user.to_string()), host),
        None => (None, authority),
    };
    let (host, port) = if let Some(stripped) = hostport.strip_prefix('[') {
        let (h, after) = stripped
            .split_once(']')
            .ok_or_else(|| format!("invalid URL {url:?}: unterminated IPv6 host"))?;
        (h, after.strip_prefix(':'))
    } else {
        match hostport.rsplit_once(':') {
            Some((h, p)) => (h, Some(p)),
            None => (hostport, None),
        }
    };
    let explicit_port = match port {
        None | Some("") => None,
        Some(p) => Some(
            p.parse::<u16>()
                .map_err(|_| format!("invalid URL {url:?}: bad port"))?,
        ),
    };
    if host.is_empty() {
        return Err(format!("invalid URL {url:?}: no host"));
    }
    let tail = tail.split('#').next().unwrap_or("");
    let target = if tail.is_empty() {
        "/".to_string()
    } else if tail.starts_with('?') {
        format!("/{tail}")
    } else {
        tail.to_string()
    };
    Ok(Url {
        scheme: scheme.to_ascii_lowercase(),
        host: host.to_ascii_lowercase(),
        explicit_port,
        target,
        userinfo,
    })
}

#[derive(Debug)]
pub enum Error {
    /// Could not connect, or the connection broke (Requests `ConnectionError`).
    Connection(String),
    /// A connect or read deadline expired (Requests `Timeout`).
    Timeout(String),
    /// The request itself is unusable (bad URL, header, proxy scheme).
    Invalid(String),
}

impl Error {
    /// Whether the network (not the request) failed: `proxy_mode=auto` retries direct.
    pub fn is_network(&self) -> bool {
        !matches!(self, Error::Invalid(_))
    }

    /// The message without its kind (Python `str(e)`).
    pub fn detail(&self) -> &str {
        match self {
            Error::Connection(d) | Error::Timeout(d) | Error::Invalid(d) => d,
        }
    }

    pub fn from_io(error: io::Error) -> Self {
        match error.kind() {
            io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock => {
                Error::Timeout(format!("read timed out ({error})"))
            }
            _ => Error::Connection(error.to_string()),
        }
    }
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Error::Connection(d) => write!(f, "ConnectionError: {d}"),
            Error::Timeout(d) => write!(f, "Timeout: {d}"),
            Error::Invalid(d) => write!(f, "InvalidRequest: {d}"),
        }
    }
}

pub struct Options<'a> {
    /// `http://[user:pass@]host:port`; None connects directly.
    pub proxy: Option<&'a str>,
    pub connect_timeout: Duration,
    pub read_timeout: Duration,
}

pub struct Response {
    pub status: u16,
    /// Header fields as received (name case kept, duplicates separate).
    pub headers: Vec<(String, String)>,
    body: Box<dyn Read + Send>,
}

impl Response {
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(n, _)| n.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    }

    /// Requests' `resp.headers.items()`: duplicate fields joined with ", ",
    /// first-seen name case, in first-seen order.
    pub fn merged_headers(&self) -> Vec<(String, String)> {
        let mut merged: Vec<(String, String)> = Vec::new();
        for (name, value) in &self.headers {
            match merged
                .iter_mut()
                .find(|(n, _)| n.eq_ignore_ascii_case(name))
            {
                Some((_, existing)) => {
                    existing.push_str(", ");
                    existing.push_str(value);
                }
                None => merged.push((name.clone(), value.clone())),
            }
        }
        merged
    }

    /// The decoded body reader.
    pub fn into_body(self) -> Box<dyn Read + Send> {
        self.body
    }
}

trait Io: Read + Write + Send {}
impl<T: Read + Write + Send> Io for T {}

fn dial(host: &str, port: u16, timeout: Duration) -> Result<TcpStream, Error> {
    let addrs: Vec<_> = (host, port)
        .to_socket_addrs()
        .map_err(|e| Error::Connection(format!("failed to resolve {host}: {e}")))?
        .collect();
    let mut last = Error::Connection(format!("no address for {host}"));
    for addr in addrs {
        match TcpStream::connect_timeout(&addr, timeout) {
            Ok(stream) => return Ok(stream),
            Err(e) if e.kind() == io::ErrorKind::TimedOut => {
                last = Error::Timeout(format!("connect to {host}:{port} timed out"))
            }
            Err(e) => last = Error::Connection(format!("connect to {host}:{port} failed: {e}")),
        }
    }
    Err(last)
}

fn percent_decode(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            let hex = std::str::from_utf8(&bytes[i + 1..i + 3]).unwrap_or("");
            if let Ok(value) = u8::from_str_radix(hex, 16) {
                out.push(value);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn proxy_authorization(proxy: &Url) -> Option<String> {
    use base64::Engine;
    let userinfo = proxy.userinfo.as_deref()?;
    let (user, pass) = userinfo.split_once(':').unwrap_or((userinfo, ""));
    let pair = format!("{}:{}", percent_decode(user), percent_decode(pass));
    Some(format!(
        "Basic {}",
        base64::engine::general_purpose::STANDARD.encode(pair)
    ))
}

pub fn parse_proxy(proxy: &str) -> Result<Url, Error> {
    let text = if proxy.contains("://") {
        proxy.to_string()
    } else {
        format!("http://{proxy}")
    };
    let url = parse_url(&text).map_err(Error::Invalid)?;
    if url.scheme != "http" {
        return Err(Error::Invalid(format!(
            "unsupported proxy scheme {}",
            url.scheme
        )));
    }
    Ok(url)
}

fn read_line(reader: &mut impl BufRead) -> Result<String, Error> {
    let mut line = Vec::new();
    reader
        .by_ref()
        .take(MAX_HEADER_LINE as u64 + 1)
        .read_until(b'\n', &mut line)
        .map_err(Error::from_io)?;
    if line.len() > MAX_HEADER_LINE {
        return Err(Error::Connection("header line too long".into()));
    }
    if line.is_empty() {
        return Err(Error::Connection(
            "remote end closed connection without response".into(),
        ));
    }
    Ok(String::from_utf8_lossy(&line)
        .trim_end_matches(['\r', '\n'])
        .to_string())
}

fn read_head(reader: &mut impl BufRead) -> Result<(u16, Vec<(String, String)>), Error> {
    let status_line = read_line(reader)?;
    let mut parts = status_line.splitn(3, ' ');
    let version = parts.next().unwrap_or("");
    let status = parts.next().and_then(|s| s.parse::<u16>().ok());
    let Some(status) = status.filter(|_| version.starts_with("HTTP/")) else {
        return Err(Error::Connection(format!(
            "bad status line {:?}",
            crate::util::truncate_chars(&status_line, 80)
        )));
    };
    let mut headers = Vec::new();
    loop {
        let line = read_line(reader)?;
        if line.is_empty() {
            break;
        }
        if headers.len() >= MAX_HEADERS {
            return Err(Error::Connection(format!(
                "got more than {MAX_HEADERS} headers"
            )));
        }
        if let Some((name, value)) = line.split_once(':') {
            headers.push((name.trim().to_string(), value.trim().to_string()));
        }
    }
    Ok((status, headers))
}

fn tunnel(tcp: &mut TcpStream, target: &Url, proxy: &Url) -> Result<(), Error> {
    let mut request = format!("CONNECT {0} HTTP/1.1\r\nHost: {0}\r\n", target.host_port());
    if let Some(auth) = proxy_authorization(proxy) {
        request.push_str(&format!("Proxy-Authorization: {auth}\r\n"));
    }
    request.push_str("\r\n");
    tcp.write_all(request.as_bytes()).map_err(Error::from_io)?;
    // Byte-wise so no TLS bytes after the proxy's answer are consumed.
    let mut head = Vec::new();
    let mut byte = [0u8; 1];
    while !head.ends_with(b"\r\n\r\n") {
        if head.len() > MAX_HEADER_LINE {
            return Err(Error::Connection("proxy answer too long".into()));
        }
        match tcp.read(&mut byte).map_err(Error::from_io)? {
            0 => return Err(Error::Connection("proxy closed the tunnel".into())),
            _ => head.push(byte[0]),
        }
    }
    let (status, _) = read_head(&mut &head[..])?;
    if !(200..300).contains(&status) {
        return Err(Error::Connection(format!(
            "Unable to connect to proxy: tunnel answered {status}"
        )));
    }
    Ok(())
}

#[cfg(target_os = "linux")]
fn tls(tcp: TcpStream, host: &str) -> Result<Box<dyn Io>, Error> {
    use std::sync::{Arc, OnceLock};
    static CONFIG: OnceLock<Result<Arc<rustls::ClientConfig>, String>> = OnceLock::new();
    let config = CONFIG
        .get_or_init(|| {
            let roots = rustls::RootCertStore {
                roots: webpki_roots::TLS_SERVER_ROOTS.to_vec(),
            };
            rustls::ClientConfig::builder_with_provider(Arc::new(
                rustls::crypto::ring::default_provider(),
            ))
            .with_safe_default_protocol_versions()
            .map(|b| Arc::new(b.with_root_certificates(roots).with_no_client_auth()))
            .map_err(|e| e.to_string())
        })
        .clone()
        .map_err(Error::Connection)?;
    let name = rustls::pki_types::ServerName::try_from(host.to_string())
        .map_err(|e| Error::Invalid(format!("bad TLS server name {host:?}: {e}")))?;
    let connection = rustls::ClientConnection::new(config, name)
        .map_err(|e| Error::Connection(format!("TLS setup failed: {e}")))?;
    Ok(Box::new(rustls::StreamOwned::new(connection, tcp)))
}

#[cfg(not(target_os = "linux"))]
fn tls(tcp: TcpStream, host: &str) -> Result<Box<dyn Io>, Error> {
    let connector = native_tls::TlsConnector::new()
        .map_err(|e| Error::Connection(format!("TLS setup failed: {e}")))?;
    let stream = connector
        .connect(host, tcp)
        .map_err(|e| Error::Connection(format!("TLS handshake failed: {e}")))?;
    Ok(Box::new(stream))
}

fn valid_header(name: &str, value: &str) -> bool {
    !name.is_empty()
        && name
            .bytes()
            .all(|b| b.is_ascii_graphic() && !b"()<>@,;:\\\"/[]?={}".contains(&b))
        && !value.bytes().any(|b| b == b'\r' || b == b'\n' || b == 0)
        && !value.starts_with([' ', '\t'])
}

fn request_head(
    method: &str,
    url: &Url,
    headers: &[(String, String)],
    body_len: usize,
    via_proxy: Option<&Url>,
) -> Result<String, Error> {
    let target = match via_proxy {
        Some(_) if url.scheme == "http" => url.absolute(),
        _ => url.target.clone(),
    };
    let has = |name: &str| headers.iter().any(|(n, _)| n.eq_ignore_ascii_case(name));
    let mut head = format!("{method} {target} HTTP/1.1\r\n");
    if !has("host") {
        head.push_str(&format!("Host: {}\r\n", url.authority()));
    }
    let defaults = [
        ("User-Agent", concat!("Tofu/", env!("CARGO_PKG_VERSION"))),
        ("Accept-Encoding", "gzip, deflate"),
        ("Accept", "*/*"),
        ("Connection", "close"),
    ];
    for (name, value) in defaults {
        if !has(name) {
            head.push_str(&format!("{name}: {value}\r\n"));
        }
    }
    for (name, value) in headers {
        if name.eq_ignore_ascii_case("content-length")
            || name.eq_ignore_ascii_case("transfer-encoding")
        {
            continue;
        }
        if !valid_header(name, value) {
            return Err(Error::Invalid(format!("invalid header {name:?}")));
        }
        head.push_str(&format!("{name}: {value}\r\n"));
    }
    if body_len > 0 || method == "POST" {
        head.push_str(&format!("Content-Length: {body_len}\r\n"));
    }
    if url.scheme == "http" {
        if let Some(auth) = via_proxy.and_then(proxy_authorization) {
            head.push_str(&format!("Proxy-Authorization: {auth}\r\n"));
        }
    }
    head.push_str("\r\n");
    Ok(head)
}

/// Send one request and read the response head. `on_socket` sees the TCP
/// socket right after it connects (before TLS and before the request).
pub fn send(
    method: &str,
    url: &Url,
    headers: &[(String, String)],
    body: &[u8],
    options: &Options,
    on_socket: &mut dyn FnMut(&TcpStream),
) -> Result<Response, Error> {
    if url.scheme != "http" && url.scheme != "https" {
        return Err(Error::Invalid(format!(
            "No connection adapters were found for {:?}",
            url.absolute()
        )));
    }
    let proxy = options.proxy.map(parse_proxy).transpose()?;
    let head = request_head(method, url, headers, body.len(), proxy.as_ref())?;
    let (host, port) = match &proxy {
        Some(p) => (p.host.clone(), p.port()),
        None => (url.host.clone(), url.port()),
    };
    let mut tcp = dial(&host, port, options.connect_timeout)?;
    let _ = tcp.set_nodelay(true);
    tcp.set_read_timeout(Some(options.read_timeout))
        .and_then(|_| tcp.set_write_timeout(Some(options.read_timeout)))
        .map_err(Error::from_io)?;
    on_socket(&tcp);
    let mut io: Box<dyn Io> = if url.scheme == "https" {
        if let Some(proxy) = &proxy {
            tunnel(&mut tcp, url, proxy)?;
        }
        tls(tcp, &url.host)?
    } else {
        Box::new(tcp)
    };
    io.write_all(head.as_bytes())
        .and_then(|_| io.write_all(body))
        .and_then(|_| io.flush())
        .map_err(Error::from_io)?;
    let mut reader = BufReader::new(io);
    let (status, headers) = loop {
        let (status, headers) = read_head(&mut reader)?;
        if (100..200).contains(&status) && status != 101 {
            continue;
        }
        break (status, headers);
    };
    let header = |name: &str| {
        headers
            .iter()
            .find(|(n, _)| n.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.trim().to_ascii_lowercase())
    };
    let raw: Box<dyn Read + Send> =
        if method == "HEAD" || (100..200).contains(&status) || status == 204 || status == 304 {
            Box::new(io::empty())
        } else if header("transfer-encoding").is_some_and(|te| te.ends_with("chunked")) {
            Box::new(Chunked::new(reader))
        } else if let Some(length) = header("content-length").and_then(|v| v.parse::<u64>().ok()) {
            Box::new(Exact {
                inner: reader,
                remaining: length,
            })
        } else {
            Box::new(UntilClose(reader))
        };
    let body = decode(raw, header("content-encoding").as_deref())?;
    Ok(Response {
        status,
        headers,
        body,
    })
}

fn decode(
    raw: Box<dyn Read + Send>,
    encoding: Option<&str>,
) -> Result<Box<dyn Read + Send>, Error> {
    match encoding {
        Some("gzip") | Some("x-gzip") => {
            let mut buffered = BufReader::new(raw);
            if buffered.fill_buf().map_err(Error::from_io)?.is_empty() {
                return Ok(Box::new(io::empty()));
            }
            Ok(Box::new(flate2::bufread::MultiGzDecoder::new(buffered)))
        }
        Some("deflate") => {
            let mut buffered = BufReader::new(raw);
            let start = buffered.fill_buf().map_err(Error::from_io)?;
            if start.is_empty() {
                return Ok(Box::new(io::empty()));
            }
            // RFC 1950 header check, as urllib3's DeflateDecoder falls back to raw deflate.
            let zlib = start.len() >= 2
                && start[0] & 0x0f == 8
                && (u16::from(start[0]) << 8 | u16::from(start[1])) % 31 == 0;
            if zlib {
                Ok(Box::new(flate2::bufread::ZlibDecoder::new(buffered)))
            } else {
                Ok(Box::new(flate2::bufread::DeflateDecoder::new(buffered)))
            }
        }
        _ => Ok(raw),
    }
}

struct Exact<R> {
    inner: R,
    remaining: u64,
}

impl<R: Read> Read for Exact<R> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        if self.remaining == 0 || buf.is_empty() {
            return Ok(0);
        }
        let want = buf.len().min(self.remaining as usize);
        let n = self.inner.read(&mut buf[..want])?;
        if n == 0 {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                format!("IncompleteRead: {} bytes missing", self.remaining),
            ));
        }
        self.remaining -= n as u64;
        Ok(n)
    }
}

struct UntilClose<R>(R);

impl<R: Read> Read for UntilClose<R> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        match self.0.read(buf) {
            // A TLS peer that closes without close_notify ends an EOF-delimited body.
            Err(e) if e.kind() == io::ErrorKind::UnexpectedEof => Ok(0),
            other => other,
        }
    }
}

struct Chunked<R> {
    inner: R,
    remaining: u64,
    done: bool,
}

impl<R: BufRead> Chunked<R> {
    fn new(inner: R) -> Self {
        Self {
            inner,
            remaining: 0,
            done: false,
        }
    }

    fn line(&mut self) -> io::Result<String> {
        let mut line = Vec::new();
        (&mut self.inner)
            .take(MAX_HEADER_LINE as u64 + 1)
            .read_until(b'\n', &mut line)?;
        if line.is_empty() || !line.ends_with(b"\n") {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "IncompleteRead: chunked body ended early",
            ));
        }
        Ok(String::from_utf8_lossy(&line).trim().to_string())
    }
}

impl<R: BufRead> Read for Chunked<R> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        if self.done || buf.is_empty() {
            return Ok(0);
        }
        if self.remaining == 0 {
            let line = self.line()?;
            let size = line.split(';').next().unwrap_or("").trim();
            let size = u64::from_str_radix(size, 16)
                .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "invalid chunk length"))?;
            if size == 0 {
                while !self.line()?.is_empty() {}
                self.done = true;
                return Ok(0);
            }
            self.remaining = size;
        }
        let want = buf.len().min(self.remaining as usize);
        let n = self.inner.read(&mut buf[..want])?;
        if n == 0 {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "IncompleteRead: chunk ended early",
            ));
        }
        self.remaining -= n as u64;
        if self.remaining == 0 {
            self.line()?;
        }
        Ok(n)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use std::net::TcpListener;

    #[test]
    fn urls_parse_like_urlparse() {
        let url = parse_url("https://user@API.OpenAI.com:8443/v1/x?y=1#frag").unwrap();
        assert_eq!(url.host, "api.openai.com");
        assert_eq!(url.explicit_port, Some(8443));
        assert_eq!(url.target, "/v1/x?y=1");
        let url = parse_url("https://api.openai.com@evil.example/").unwrap();
        assert_eq!(url.host, "evil.example");
        let url = parse_url("http://[::1]:8317").unwrap();
        assert_eq!((url.host.as_str(), url.port()), ("::1", 8317));
        assert_eq!(url.target, "/");
        assert!(parse_url("http://h:99999/").is_err());
        assert!(parse_url("no-scheme").is_err());
        assert_eq!(
            url.join("/v1/models").unwrap().absolute(),
            "http://[::1]:8317/v1/models"
        );
    }

    #[test]
    fn proxies_are_http_only_with_basic_auth() {
        let proxy = parse_proxy("user:p%40ss@proxy.example:3128").unwrap();
        assert_eq!(proxy.port(), 3128);
        assert_eq!(
            proxy_authorization(&proxy).unwrap(),
            "Basic dXNlcjpwQHNz" // user:p@ss
        );
        assert!(parse_proxy("socks5://127.0.0.1:1080").is_err());
    }

    #[test]
    fn header_injection_is_refused() {
        let url = parse_url("http://127.0.0.1:1/").unwrap();
        let bad = vec![("X-A".to_string(), "v\r\nInjected: 1".to_string())];
        assert!(request_head("GET", &url, &bad, 0, None).is_err());
        let ok = vec![("user-agent".to_string(), "mine".to_string())];
        let head = request_head("POST", &url, &ok, 0, None).unwrap();
        assert!(head.contains("user-agent: mine\r\n") && !head.contains("Tofu/"));
        assert!(head.contains("Content-Length: 0\r\n"));
    }

    fn serve_once(response: &'static [u8]) -> String {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            let (mut sock, _) = listener.accept().unwrap();
            let mut buf = [0u8; 4096];
            let _ = sock.read(&mut buf);
            sock.write_all(response).unwrap();
        });
        format!("http://127.0.0.1:{port}/p")
    }

    fn fetch(url: &str) -> (Response, Vec<u8>) {
        let options = Options {
            proxy: None,
            connect_timeout: Duration::from_secs(5),
            read_timeout: Duration::from_secs(5),
        };
        let response = send(
            "GET",
            &parse_url(url).unwrap(),
            &[],
            b"",
            &options,
            &mut |_| {},
        )
        .unwrap();
        let headers = response.headers.clone();
        let mut body = Vec::new();
        response.into_body().read_to_end(&mut body).unwrap();
        (
            Response {
                status: 0,
                headers,
                body: Box::new(io::empty()),
            },
            body,
        )
    }

    #[test]
    fn chunked_gzip_and_duplicate_headers_decode() {
        let mut gz = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
        gz.write_all(b"hello egress").unwrap();
        let packed = gz.finish().unwrap();
        let mut response = b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 200 OK\r\nSet-Cookie: a\r\nX-D: 1\r\nx-d: 2\r\nContent-Encoding: gzip\r\nTransfer-Encoding: chunked\r\n\r\n".to_vec();
        let (first, second) = packed.split_at(5);
        for part in [first, second] {
            response.extend_from_slice(format!("{:x};ext=1\r\n", part.len()).as_bytes());
            response.extend_from_slice(part);
            response.extend_from_slice(b"\r\n");
        }
        response.extend_from_slice(b"0\r\nTrailer: x\r\n\r\n");
        let (head, body) = fetch(&serve_once(Box::leak(response.into_boxed_slice())));
        assert_eq!(body, b"hello egress");
        let merged = head.merged_headers();
        assert!(merged.contains(&("X-D".to_string(), "1, 2".to_string())));
    }

    #[test]
    fn short_content_length_is_an_error() {
        let url = serve_once(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nabc");
        let options = Options {
            proxy: None,
            connect_timeout: Duration::from_secs(5),
            read_timeout: Duration::from_secs(5),
        };
        let response = send(
            "GET",
            &parse_url(&url).unwrap(),
            &[],
            b"",
            &options,
            &mut |_| {},
        )
        .unwrap();
        let mut body = Vec::new();
        assert!(response.into_body().read_to_end(&mut body).is_err());
    }

    #[test]
    fn eof_delimited_body_reads_to_close() {
        let (_, body) = fetch(&serve_once(b"HTTP/1.0 200 OK\r\n\r\nuntil close"));
        assert_eq!(body, b"until close");
    }
}
