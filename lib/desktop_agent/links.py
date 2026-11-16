"""Node link lifecycle: the desktop launcher and the headless CLI; no UI or server imports.

A node keeps one outbound bridge loop ("link") per server it joined, plus an
optional local link to the packaged app's own server. ``start``/``stop``/
``restart`` own one poll worker per link state; :class:`NodeLinks` owns the set
of links, joining and leaving servers (contracts/desktop_node_v1.schema.json).
"""
import threading


def _lock(state):
    return state.setdefault('_connection_lock', threading.RLock())


def resume(url, secret, log, candidates=(), persist=None):
    if url:
        try:
            from lib.desktop_agent._pair import resume_attachment
            return resume_attachment(url, secret, log=log, candidates=candidates,
                                     persist=persist)
        except Exception as exc:
            log('Attachment resume probe skipped: %s' % exc)
    return url, secret


def ensure_relay(state, log):
    """Create at most one browser relay per link. Local links never call this.

    Each relay admits only its own link's origins, so a signed-in page finds
    exactly the relay of the server it belongs to.
    """
    with _lock(state):
        if state.get('browser_relay') is not None:
            return
        try:
            from lib.desktop_agent._browser_relay import BrowserRelay

            def urls():
                return [state.get('url') or ''] + list(state.get('candidates') or [])

            relay = BrowserRelay(urls, log=log)
            if relay.start():
                state['browser_relay'] = relay
        except Exception as exc:
            log('Browser relay unavailable: %s' % exc)


def start(state, perms, log, *, remote=True, on_revoked=None):
    with _lock(state):
        thread = state.get('thread')
        if thread and thread.is_alive():
            return False
        url, secret = state.get('url') or '', state.get('secret') or ''
        if not url:
            state['last_status'] = {'state': 'unconfigured'}
            state['enabled'] = False
            return False
        from lib.desktop_agent import run_agent
        stop_event = threading.Event()
        state['stop'] = stop_event
        state['error'] = None

        def on_status(status):
            state['last_status'] = status
            log('Link status (%s): %s' % (state.get('label') or url, status))
            # Tofu itself refused the credential: it was revoked (or the node
            # was removed on the server). Retrying cannot succeed; stop the
            # link and let the owner join again.
            if remote and status.get('state') == 'auth':
                stop_event.set()
                state['revoked'] = True
                if on_revoked is not None:
                    try:
                        on_revoked(state)
                    except Exception as exc:
                        log('Could not record revoked link: %s' % exc)

        def route_repair():
            current = state.get('url') or ''
            credential = state.get('secret') or ''
            new_url, new_secret = resume(
                current, credential, log, candidates=state.get('candidates') or (),
                persist=state.get('persist_route'))
            if stop_event.is_set():
                return None
            if new_url and (new_url, new_secret) != (current, credential):
                state['url'], state['secret'] = new_url, new_secret
                state['server_url'] = new_url
                return new_url, new_secret
            return None

        def loop():
            try:
                run_agent(url, perms, poll_interval=1.0,
                          bridge_secret=secret, stop_event=stop_event,
                          on_status=on_status,
                          route_repair=route_repair if remote else None,
                          browser_relay=state.get('browser_relay') if remote else None)
            except Exception as exc:
                state['error'] = str(exc)
                on_status({'state': 'error', 'detail': str(exc)})
                log('Agent loop crashed: %s' % exc)
            finally:
                state['enabled'] = False

        thread = threading.Thread(target=loop, daemon=True, name='tofu-desktop-agent')
        state['thread'] = thread
        state['enabled'] = True
        thread.start()
        return True


def stop(state, log, *, close_relay=True):
    """Bounded join; retain a live worker handle rather than spawn a duplicate."""
    with _lock(state):
        event = state.get('stop')
        if event is not None:
            event.set()
        state['enabled'] = False
        thread = state.get('thread')
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)
        if close_relay:
            relay = state.pop('browser_relay', None)
            if relay is not None:
                relay.close()
        stopped = not thread or not thread.is_alive()
        if not stopped:
            log('Agent is still stopping; refusing a second polling worker')
        return stopped


def restart(state, perms, log):
    with _lock(state):
        if stop(state, log, close_relay=False):
            return start(state, perms, log)
        return False


LOCAL_LINK = 'local'


class NodeLinks:
    """All links of this node: the local server and every joined server.

    ``perms`` is the one live permission dict every link shares; tray toggles
    mutate it in place and each poll re-declares it to its server.
    """

    def __init__(self, perms, log):
        self.perms = perms
        self.log = log
        self._lock = threading.RLock()
        self._links = {}
        self.running = False

    def _start_attachment(self, record):
        from lib.desktop_agent.config import update_attachment
        att_id = record['id']
        state = {'url': record['url'], 'secret': record['secret'],
                 'server_url': record['url'],
                 'label': record.get('label') or record['url'],
                 'candidates': list(record.get('candidates') or []),
                 'attachment_id': att_id,
                 'persist_route': lambda url: update_attachment(att_id, url=url)}
        self._links[att_id] = state
        ensure_relay(state, self.log)
        start(state, self.perms, self.log, remote=True,
              on_revoked=lambda _s: update_attachment(att_id, state='revoked'))

    def start(self, local=None):
        """Start the local link (``(url, secret)``) and every active attachment."""
        from lib.desktop_agent.config import attachments
        with self._lock:
            self.running = True
            if local is not None and LOCAL_LINK not in self._links:
                url, secret = local
                state = {'url': url, 'secret': secret, 'server_url': url,
                         'label': url}
                self._links[LOCAL_LINK] = state
                start(state, self.perms, self.log, remote=False)
            for record in attachments():
                if (record.get('state', 'active') == 'active'
                        and record['id'] not in self._links):
                    self._start_attachment(record)

    def stop(self):
        with self._lock:
            self.running = False
            links = list(self._links.values())
            self._links.clear()
        return all([stop(state, self.log) for state in links])

    def join(self, text, *, node_id, name='', stop=None):
        """Redeem one pairing input and, if the node is running, link at once."""
        from lib.desktop_agent.attachments import join
        record = join(text, node_id=node_id, name=name, log=self.log, stop=stop)
        with self._lock:
            old = self._links.pop(record['id'], None)
            if old is not None:
                stop(old, self.log)
            if self.running:
                self._start_attachment(record)
        return record

    def leave(self, attachment_id):
        from lib.desktop_agent.config import remove_attachment
        with self._lock:
            state = self._links.pop(attachment_id, None)
        if state is not None:
            stop(state, self.log)
        return remove_attachment(attachment_id)

    def statuses(self):
        """One row per joined server (and the local link while running)."""
        from lib.desktop_agent.config import attachments
        with self._lock:
            links = dict(self._links)
        rows = []
        local = links.get(LOCAL_LINK)
        if local is not None:
            rows.append({'id': LOCAL_LINK, 'label': local['label'], 'local': True,
                         'state': 'active', 'enabled': bool(local.get('enabled')),
                         'last_status': local.get('last_status') or {}})
        for record in attachments():
            state = links.get(record['id']) or {}
            revoked = state.get('revoked') or record.get('state') == 'revoked'
            rows.append({'id': record['id'],
                         'label': record.get('label') or record['url'],
                         'url': state.get('url') or record['url'], 'local': False,
                         'state': 'revoked' if revoked else 'active',
                         'enabled': bool(state.get('enabled')),
                         'last_status': state.get('last_status') or {}})
        return rows
