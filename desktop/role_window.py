#!/usr/bin/env python3
"""desktop/role_window.py — the startup role window + control panel.

The startup-role contract (docs/modules/remote_execution.md): the desktop
app says at startup what this machine is — a Tofu node running its own
server, usable by that server and by every server it joined — and the
node controls (computer-control switch, permission tiers, joined servers,
login startup) do not live ONLY in the system tray.

Deliberately split so headless CI can test every fact the window shows:

* **Pure builder** — :func:`role_state_full` returns a plain dict of localized display facts. The tests assert the
  SENTENCES, never pixels.
* **The gate** — :func:`should_show_at_startup` reads ``show_role_window``
  from the agent config blob. Absent key (= fresh install AND upgrades
  from pre-window builds) means SHOW: nobody unchecks a window they have
  never seen.
* **The renderer** — :func:`show_role_window`, tkinter imported lazily
  (headless rule), themed by ``_tk_theme.apply_theme``, one re-entrant
  instance per process: a second call lifts the existing window instead
  of stacking duplicates.

Window ownership (owner report 2026-08-04 — "minimize to tray" stranded
the app with NO tray): the renderer has two modes picked at call time.

* **Host-backed** (``_tk_host.parent_or_none()`` non-None — the Windows
  tray-first topology): the window is a Toplevel of the host root, this
  function returns immediately, and BOTH the title-bar minimize button
  and the in-window button hide it to the ALREADY-RUNNING tray.
* **Standalone** (legacy sequence / first-run / host failed): its own Tk
  + blocking mainloop, exactly the old behaviour — minimizing to a tray
  that does not exist yet would strand the app, so the interception stays
  off.

The renderer never owns state: it pulls fresh facts from ``state_fn`` on
every refresh and delegates every mutation to ``actions`` — the same
seams the tray menus already call, so window and tray can never disagree
about what a click does.
"""

from lib.log import get_logger

logger = get_logger(__name__)


def _noop_log(_msg: str) -> None:
    pass


# ═══════════════════════════════════════════════════════════════
#  The startup gate
# ═══════════════════════════════════════════════════════════════

def should_show_at_startup() -> bool:
    """Whether the role window appears at launch.

    ABSENT key → True. That covers fresh installs AND every upgrade from
    a build predating this window — both are users who have never seen
    the panel, which is exactly the audience the window exists for. Any
    read failure also means True: a hidden panel is the failure mode this
    module was written to kill.
    """
    try:
        from lib.desktop_agent.config import load_config
        value = load_config().get('show_role_window')
        return True if value is None else bool(value)
    except Exception as e:
        logger.warning('Could not read show_role_window, defaulting to '
                       'show: %s', e)
        return True


def persist_show_at_startup(flag: bool) -> None:
    """Persist the window's "show at startup" checkbox.

    Merges into the existing config blob (agent_id, attachments, …) —
    a clobbering write would erase the attachment the window itself just
    displayed. Failures are logged, never fatal.
    """
    try:
        from lib.desktop_agent.config import update_config

        def _mutate(cfg):
            cfg['show_role_window'] = bool(flag)
            return cfg

        update_config(_mutate)
    except Exception as e:
        logger.warning('Could not persist show_role_window: %s', e)


# ═══════════════════════════════════════════════════════════════
#  Pure builders — every fact the window shows, headless-testable
# ═══════════════════════════════════════════════════════════════

def _server_status(server_state, exit_code, lang):
    """(tone, label, detail) for the server card's status row."""
    from desktop import _tk_theme as theme
    if server_state == 'stopped':
        detail = theme.t('desktop.role.serverStoppedDetail', lang).replace(
            '{code}', str(exit_code if exit_code is not None else '?'))
        return 'error', theme.t('desktop.role.serverStopped', lang), detail
    if server_state == 'starting':
        return 'muted', theme.t('desktop.role.serverStarting', lang), ''
    return 'ok', theme.t('desktop.role.serverRunning', lang), ''


def _cc_status(cc_state, lang):
    """(tone, sentence) for the full app's computer-control status row.

    The agent polls this app's own server, so link failures are phrased
    for the local case — the agent app's "re-run the installer" advice
    would be wrong here.
    """
    from desktop import _tk_theme as theme
    cc_state = cc_state or {}
    if not cc_state.get('enabled'):
        error = str(cc_state.get('error') or '').strip()
        if error:
            return 'error', theme.t('desktop.role.ccFailed', lang).replace(
                '{detail}', error[:160])
        return 'muted', theme.t('desktop.role.ccOff', lang)
    status = cc_state.get('last_status') or {}
    code = status.get('state')
    if code == 'ok':
        return 'ok', theme.t('desktop.role.linkOk', lang)
    if not code:
        return 'muted', theme.t('desktop.role.linkWaiting', lang)
    if code in ('unreachable', 'proxy'):
        return 'error', theme.t('desktop.role.linkDown', lang)
    detail = ('HTTP %s' % status.get('code') if code == 'http'
              else str(status.get('detail') or code))
    return 'error', theme.t('desktop.role.linkRefused', lang).replace(
        '{detail}', detail[:120])


def _link_status_text(row, lang):
    """(tone, phrase) for one joined server's link."""
    from desktop import _tk_theme as theme
    if row.get('state') == 'revoked':
        return 'error', theme.t('desktop.tray.stAuth', lang)
    if not row.get('enabled'):
        return 'muted', theme.t('desktop.tray.stOff', lang)
    status = row.get('last_status') or {}
    code = status.get('state')
    if code == 'ok':
        key = 'desktop.tray.stOkBrowser' if status.get('transport') == 'browser' \
            else 'desktop.tray.stOk'
        return 'ok', theme.t(key, lang)
    if not code:
        return 'muted', theme.t('desktop.tray.stStarting', lang)
    if code == 'auth':
        return 'error', theme.t('desktop.tray.stAuth', lang)
    if code == 'proxy':
        return 'error', theme.t('desktop.tray.stProxy', lang)
    if code == 'unreachable':
        return 'error', theme.t('desktop.tray.stUnreachable', lang)
    if code == 'http':
        return 'error', theme.t('desktop.tray.stHttp', lang).replace(
            '{code}', str(status.get('code') or '?'))
    return 'error', theme.t('desktop.tray.stError', lang).replace(
        '{detail}', str(status.get('detail') or code)[:120])


def server_rows(links, lang=None) -> list:
    """Display rows for the joined servers (local link excluded)."""
    from desktop import _tk_theme as theme
    lang = lang or theme.detect_lang()
    rows = []
    for row in links or []:
        if row.get('local'):
            continue
        tone, phrase = _link_status_text(row, lang)
        rows.append({'id': row['id'], 'label': row.get('label') or '',
                     'tone': tone, 'status': phrase,
                     'text': theme.t('desktop.tray.serverRow', lang)
                     .replace('{label}', row.get('label') or '')
                     .replace('{status}', phrase)})
    return rows


def role_state_full(port, cc_state, links=(), show_flag=True,
                    lang=None, server_state='running',
                    server_exit_code=None, autostart=None) -> dict:
    """Display facts for the node's role window.

    ``links`` are :meth:`lib.desktop_agent.links.NodeLinks.statuses` rows;
    every joined server is listed with its live link verdict so a revoked
    or unreachable server is never invisible. ``server_state`` ('starting'
    | 'running' | 'stopped') is the local server child's liveness.
    ``autostart=None`` means the platform has no login-startup support and
    the row is hidden.
    """
    from desktop import _tk_theme as theme
    lang = lang or theme.detect_lang()
    perms = (cc_state or {}).get('perms') or {}
    server_tone, server_label, server_detail = _server_status(
        server_state, server_exit_code, lang)
    cc_tone, cc_text = _cc_status(cc_state, lang)
    return {
        'server_tone': server_tone,
        'server_label': server_label,
        'server_detail': server_detail,
        'cc_tone': cc_tone,
        'cc_text': cc_text,
        'kind': 'full',
        'lang': lang,
        'role': theme.t('desktop.role.serverTitle', lang),
        'server_url': 'http://127.0.0.1:%d' % port,
        'servers': server_rows(links, lang),
        'cc_enabled': bool((cc_state or {}).get('enabled')),
        'perms': {k: bool(v) for k, v in perms.items()},
        'tiers': ['allow_write', 'allow_exec', 'allow_gui', 'allow_egress',
                  'allow_codex_login'],
        'autostart': autostart,
        'show_at_startup': bool(show_flag),
    }


# ═══════════════════════════════════════════════════════════════
#  The renderer — lazy tkinter, one re-entrant instance
# ═══════════════════════════════════════════════════════════════

_OPEN = {'root': None, 'refresh': None}

# The panel re-pulls state_fn on this cadence so a crashed server child or a
# changing agent link shows without a click (the agent window's existing
# link tick, now shared by both kinds).
_REFRESH_MS = 3000

_TONE_COLORS = {'ok': 'success', 'error': 'error', 'muted': 'text3'}

_TIER_KEYS = {
    'allow_write': 'permWrite', 'allow_exec': 'permExec',
    'allow_gui': 'permGui', 'allow_egress': 'permEgress',
    'allow_codex_login': 'permCodexLogin',
}

_TIER_DESC_KEYS = {
    'allow_write': 'desktop.role.tierWriteDesc',
    'allow_exec': 'desktop.role.tierExecDesc',
    'allow_gui': 'desktop.role.tierGuiDesc',
    'allow_egress': 'desktop.role.tierEgressDesc',
    'allow_codex_login': 'desktop.role.tierCodexLoginDesc',
}


def show_role_window(state_fn, actions, log=_noop_log) -> None:
    """Show the role window; host-backed mode returns immediately.

    Args:
        state_fn: zero-arg callable returning a fresh :func:`role_state_full`
            dict on every refresh (the window never caches facts).
        actions: mutation callbacks: 'open'(), 'open_log'(), 'toggle_cc'(),
            'toggle_perm'(key), 'join'(), 'leave'(attachment_id) and
            'toggle_autostart'(). Every action is followed by a refresh.
        log: launcher-style diagnostic sink.

    Widgets are built once and updated in place by ``_refresh`` (an action
    or the periodic tick), so live facts never rebuild the layout.

    Re-entrant: a second call while open lifts and refreshes the existing
    window — the tray's "Control panel…" item relies on this.
    """
    from desktop import _tk_theme as theme
    from desktop import _tk_host as host
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError as e:
        log('Role window unavailable (no tkinter): %s' % e)
        logger.warning('Role window unavailable (no tkinter): %s', e)
        return

    existing = _OPEN.get('root')
    if existing is not None:
        try:
            existing.deiconify()
            existing.lift()
            existing.focus_force()
            refresh = _OPEN.get('refresh')
            if refresh is not None:
                refresh()
            return
        except tk.TclError:
            _OPEN['root'] = None
            _OPEN['refresh'] = None

    parent = host.parent_or_none()

    state = state_fn()
    lang = state.get('lang') or theme.detect_lang()

    if parent is not None:
        root = tk.Toplevel(parent)
    else:
        root = tk.Tk()
    _OPEN['root'] = root
    palette = theme.apply_theme(root)
    root.title('Tofu')
    root.resizable(False, False)
    theme.set_window_icon(root)
    root.protocol('WM_DELETE_WINDOW', lambda: _close())
    # Fonts follow the DPI-aware tk scaling; pixel widths must follow too
    # or text wraps early and the window is cramped above 100% scaling.
    scale = theme.ui_scale(root)

    def px(value):
        return round(value * scale)

    frame = ttk.Frame(root, style='Tofu.TFrame', padding=(28, 24))
    frame.grid(sticky='nsew')
    frame.columnconfigure(0, weight=1)
    root.columnconfigure(0, weight=1)

    # ── Header: brand + role sentence + plain-language subtitle ──
    header = ttk.Frame(frame, style='Tofu.TFrame')
    header.grid(row=0, column=0, sticky='we')
    photo = theme.load_logo_photo(root, size=44)
    if photo is not None:
        ttk.Label(header, image=photo, style='Tofu.TLabel').grid(
            row=0, column=0, rowspan=2, padx=(0, 14))
    ttk.Label(header, text=state['role'],
              style='Tofu.Title.TLabel').grid(row=0, column=1, sticky='sw')
    ttk.Label(header, text=theme.t('desktop.role.serverSub', lang), wraplength=px(540),
              justify='left', style='Tofu.Sub.TLabel').grid(
        row=1, column=1, sticky='nw', pady=(2, 0))

    show_state = {'on': bool(state.get('show_at_startup', True))}

    def _act(name, *args):
        fn = actions.get(name)
        if fn is None:
            return
        try:
            fn(*args)
        except Exception as e:
            log('Role-window action %s failed: %s' % (name, e))
            logger.warning('Role-window action %s failed: %s', name, e)
        _refresh()

    def _persist():
        persist_show_at_startup(show_state['on'])

    def _close():
        _persist()
        _OPEN['root'] = None
        _OPEN['refresh'] = None
        try:
            root.destroy()
        except tk.TclError:
            pass

    def _on_unmap(event):
        """Title-bar minimize = minimize to tray (host-backed mode only).

        The tray icon exists before this window ever could hide (tray-
        first topology), so converting the taskbar-iconify into a withdraw
        keeps the design-doc promise「minimizing sends it to the tray」.
        Standalone mode has no tray yet — the interception stays OFF and
        the button keeps its stock taskbar meaning.
        """
        if event.widget is not root:
            return
        try:
            if root.state() == 'iconic':
                _persist()
                root.withdraw()
        except tk.TclError:
            pass

    def _set_dot(widget, tone):
        color = palette[_TONE_COLORS.get(tone, 'text3')]
        image = theme.dot_image(root, color)
        if image is not None:
            widget.configure(image=image, text='')
        else:
            widget.configure(text='\u25cf', fg=color)

    def _clickable(widgets, target):
        for widget in widgets:
            widget.bind('<Button-1>', lambda _e: target.invoke())

    # ── Server card: live status, address, primary action ──
    card = theme.card_frame(frame, palette)
    card.grid(row=1, column=0, sticky='we', pady=(20, 12))
    card.columnconfigure(1, weight=1)
    ttk.Label(card, text=theme.t('desktop.role.serverCardLabel', lang),
              style='CardHead.TLabel').grid(row=0, column=0, columnspan=3,
                                            sticky='w', padx=18, pady=(14, 6))
    server_dot = tk.Label(card, bg=palette['bg2'], bd=0)
    server_dot.grid(row=1, column=0, sticky='w', padx=(18, 8))
    server_line = ttk.Frame(card, style='Card.TFrame')
    server_line.grid(row=1, column=1, sticky='w')
    server_label = ttk.Label(server_line, style='CardName.TLabel')
    server_label.grid(row=0, column=0, sticky='w')
    server_url = ttk.Label(server_line, style='CardUrl.TLabel')
    server_url.grid(row=0, column=1, sticky='w', padx=(10, 0))
    ttk.Button(card, text=theme.t('desktop.tray.open', lang),
               style='Tofu.Accent.TButton',
               command=lambda: _act('open')).grid(
        row=1, column=2, sticky='e', padx=(12, 18))
    server_detail = ttk.Label(card, style='Status.Err.TLabel',
                              wraplength=px(580), justify='left')
    log_button = None
    if actions.get('open_log'):
        log_button = ttk.Button(card, text=theme.t('desktop.role.openLog', lang),
                                style='Tofu.Small.TButton',
                                command=lambda: _act('open_log'))
    tk.Frame(card, bg=palette['bg2'], height=14).grid(row=9, column=0)

    # ── Computer-control card ──
    cc = theme.card_frame(frame, palette)
    cc.grid(row=2, column=0, sticky='we')
    cc.columnconfigure(0, weight=1)
    ttk.Label(cc, text=theme.t('desktop.role.ccTitle', lang),
              style='CardTitle.TLabel').grid(row=0, column=0, sticky='w',
                                             padx=18, pady=(16, 4))
    cc_switch = theme.Switch(cc, root, palette, palette['bg2'],
                             lambda: _act('toggle_cc'))
    cc_switch.widget.grid(row=0, column=1, sticky='e', padx=18,
                          pady=(16, 4))
    status_row = ttk.Frame(cc, style='Card.TFrame')
    status_row.grid(row=1, column=0, columnspan=2, sticky='we', padx=18,
                    pady=(0, 14))
    cc_dot = tk.Label(status_row, bg=palette['bg2'], bd=0)
    cc_dot.grid(row=0, column=0, sticky='nw', padx=(0, 8), pady=(4, 0))
    cc_text = ttk.Label(status_row, wraplength=px(560), justify='left',
                        style='Status.Muted.TLabel')
    cc_text.grid(row=0, column=1, sticky='w')
    tk.Frame(cc, bg=palette['border'], height=1).grid(
        row=2, column=0, columnspan=2, sticky='we', padx=18)

    perm_grid = ttk.Frame(cc, style='Card.TFrame')
    perm_grid.grid(row=3, column=0, columnspan=2, sticky='we', padx=18,
                   pady=(14, 0))
    perm_grid.columnconfigure(0, weight=1, uniform='perm')
    perm_grid.columnconfigure(1, weight=1, uniform='perm')
    tiles = {}
    for index, key in enumerate(state['tiers']):
        tile = tk.Frame(perm_grid, bg=palette['bg3'], padx=14, pady=12)
        tile.grid(row=index // 2, column=index % 2, sticky='nsew',
                  padx=(0, 5) if index % 2 == 0 else (5, 0), pady=(0, 10))
        tile.columnconfigure(0, weight=1)
        name = ttk.Label(tile, text=theme.t('desktop.tray.' + _TIER_KEYS[key],
                                            lang),
                         style='TileName.TLabel', wraplength=px(190),
                         justify='left')
        name.grid(row=0, column=0, sticky='w')
        switch = theme.Switch(tile, root, palette, palette['bg3'],
                              lambda k=key: _act('toggle_perm', k))
        switch.widget.grid(row=0, column=1, sticky='ne', padx=(10, 0))
        desc = ttk.Label(tile, text=theme.t(_TIER_DESC_KEYS[key], lang),
                         style='TileSub.TLabel', wraplength=px(240),
                         justify='left')
        desc.grid(row=1, column=0, columnspan=2, sticky='w', pady=(4, 0))
        _clickable((tile, name, desc), switch)
        tiles[key] = (name, desc, switch)

    autostart_switch = None
    if state.get('autostart') is not None:
        auto_row = ttk.Frame(cc, style='Card.TFrame')
        auto_row.grid(row=4, column=0, columnspan=2, sticky='we', padx=18,
                      pady=(8, 8))
        auto_row.columnconfigure(0, weight=1)
        auto_name = ttk.Label(auto_row,
                              text=theme.t('desktop.tray.autostart', lang),
                              style='CardName.TLabel')
        auto_name.grid(row=0, column=0, sticky='w')
        auto_desc = ttk.Label(auto_row,
                              text=theme.t('desktop.role.autostartDesc', lang),
                              style='CardSub.TLabel', wraplength=px(520),
                              justify='left')
        auto_desc.grid(row=1, column=0, sticky='w')
        autostart_switch = theme.Switch(auto_row, root, palette,
                                        palette['bg2'],
                                        lambda: _act('toggle_autostart'))
        autostart_switch.widget.grid(row=0, column=1, rowspan=2, sticky='e')
        _clickable((auto_name, auto_desc), autostart_switch)
    hint = ttk.Label(cc, wraplength=px(600), justify='left',
                     style=theme.card_label_style(palette, 'CardSub.TLabel'))
    hint.grid(row=5, column=0, columnspan=2, sticky='w', padx=18,
              pady=(4, 16))

    # ── Joined servers: one row per attachment + the join action ──
    servers = theme.card_frame(frame, palette)
    servers.grid(row=3, column=0, sticky='we', pady=(12, 0))
    servers.columnconfigure(0, weight=1)
    ttk.Label(servers, text=theme.t('desktop.role.serversTitle', lang),
              style='CardTitle.TLabel').grid(row=0, column=0, sticky='w',
                                             padx=18, pady=(16, 4))
    ttk.Button(servers, text=theme.t('desktop.tray.join', lang),
               style='Tofu.Accent.TButton',
               command=lambda: _act('join')).grid(
        row=0, column=1, sticky='e', padx=18, pady=(16, 4))
    ttk.Label(servers, text=theme.t('desktop.role.serversHint', lang),
              style=theme.card_label_style(palette, 'CardSub.TLabel'),
              wraplength=px(600), justify='left').grid(
        row=1, column=0, columnspan=2, sticky='w', padx=18)
    server_list = ttk.Frame(servers, style='Card.TFrame')
    server_list.grid(row=2, column=0, columnspan=2, sticky='we', padx=18,
                     pady=(8, 16))
    server_list.columnconfigure(1, weight=1)
    server_widgets = {'key': None}

    def _render_servers(rows, st_lang):
        key = tuple((r['id'], r['text'], r['tone']) for r in rows)
        if key == server_widgets['key']:
            return
        server_widgets['key'] = key
        for child in server_list.winfo_children():
            child.destroy()
        if not rows:
            ttk.Label(server_list, text=theme.t('desktop.tray.noServers', st_lang),
                      style='CardSub.TLabel').grid(row=0, column=0, sticky='w')
            return
        for index, row in enumerate(rows):
            dot = tk.Label(server_list, bg=palette['bg2'], bd=0)
            dot.grid(row=index, column=0, sticky='w', padx=(0, 8), pady=2)
            _set_dot(dot, row['tone'])
            ttk.Label(server_list, text=row['text'], style='CardName.TLabel',
                      wraplength=px(470), justify='left').grid(
                row=index, column=1, sticky='w', pady=2)
            ttk.Button(server_list,
                       text=theme.t('desktop.role.leave', st_lang),
                       style='Tofu.Small.TButton',
                       command=lambda i=row['id']: _act('leave', i)).grid(
                row=index, column=2, sticky='e', pady=2)

    # ── Bottom bar: startup gate + the dismiss action ──
    bottom = ttk.Frame(frame, style='Tofu.TFrame')
    bottom.grid(row=4, column=0, sticky='we', pady=(18, 0))

    def _toggle_show():
        show_state['on'] = not show_state['on']
        show_switch.set(show_state['on'])
        _persist()

    show_switch = theme.Switch(bottom, root, palette, palette['bg'],
                               _toggle_show)
    show_switch.widget.grid(row=0, column=0, sticky='w', padx=(0, 8))
    show_switch.set(show_state['on'])
    show_lbl = ttk.Label(bottom, text=theme.t('desktop.role.showAtStartup',
                                              lang), style='Bg.TLabel')
    show_lbl.grid(row=0, column=1, sticky='w')
    _clickable((show_lbl,), show_switch)
    next_col = 2
    # One click copies the dead-link evidence pack (routes, candidates, link
    # verdicts, log tail) for a machine the owner cannot open a shell on. The
    # action only BUILDS the text; the clipboard write stays on the tk thread.
    if actions.get('copy_diag'):
        diag_btn = ttk.Button(bottom,
                              text=theme.t('desktop.role.copyDiag', lang),
                              style='Tofu.TButton')

        def _copy_diag():
            try:
                text = actions['copy_diag']()
            except Exception as e:
                log('Diagnostics report failed: %s' % e)
                return
            try:
                root.clipboard_clear()
                root.clipboard_append(text)
            except tk.TclError as e:
                log('Clipboard set failed: %s' % e)
                return
            diag_btn.config(text=theme.t('desktop.role.copyDiagDone', lang))
            try:
                root.after(2000, lambda: diag_btn.config(
                    text=theme.t('desktop.role.copyDiag', lang)))
            except tk.TclError:
                pass

        diag_btn.config(command=_copy_diag)
        diag_btn.grid(row=0, column=next_col, sticky='e', padx=(0, 8))
        next_col += 1
    ttk.Button(bottom, text=theme.t('desktop.role.minimize', lang),
               style='Tofu.TButton',
               command=_close).grid(row=0, column=next_col, sticky='e')
    bottom.columnconfigure(1, weight=1)

    def _refresh():
        st = state_fn()
        st_lang = st.get('lang') or lang
        _set_dot(server_dot, st['server_tone'])
        server_label.config(text=st['server_label'])
        server_url.config(text=st['server_url'])
        if st.get('server_detail'):
            server_detail.config(text=st['server_detail'])
            server_detail.grid(row=2, column=0, columnspan=3, sticky='w',
                               padx=(18, 8), pady=(8, 0))
            if log_button is not None:
                log_button.grid(row=3, column=0, columnspan=3, sticky='w',
                                padx=18, pady=(10, 0))
        else:
            server_detail.grid_remove()
            if log_button is not None:
                log_button.grid_remove()
        cc_switch.set(st['cc_enabled'])
        cc_tone, cc_sentence = st['cc_tone'], st['cc_text']
        tiers_live = st['cc_enabled']
        hint.config(text=theme.t('desktop.role.permHint' if tiers_live
                                 else 'desktop.role.ccEnableFirst',
                                 st_lang))
        if autostart_switch is not None:
            autostart_switch.set(bool(st.get('autostart')))
        _render_servers(st.get('servers') or [], st_lang)
        _set_dot(cc_dot, cc_tone)
        cc_text.config(text=cc_sentence, style=(
            'Status.Ok.TLabel' if cc_tone == 'ok' else
            'Status.Err.TLabel' if cc_tone == 'error' else
            'Status.Muted.TLabel'))
        for key, (name, desc, switch) in tiles.items():
            switch.set(bool(st['perms'].get(key)), tiers_live)
            name.config(style='TileName.TLabel' if tiers_live
                        else 'TileName.Off.TLabel')
            desc.config(style='TileSub.TLabel' if tiers_live
                        else 'TileSub.Off.TLabel')

    def _tick():
        if _OPEN.get('root') is not root:
            return
        try:
            _refresh()
        except tk.TclError:
            return  # window already gone
        except Exception as e:
            log('Role window refresh failed: %s' % e)
        try:
            root.after(_REFRESH_MS, _tick)
        except tk.TclError:
            pass

    _OPEN['refresh'] = _refresh
    _refresh()
    root.after(_REFRESH_MS, _tick)
    theme.center_on_screen(root, width=px(680))
    if parent is not None:
        # Host-backed: the tray is already running — the title-bar
        # minimize also sends the window to it, and we never block.
        root.bind('<Unmap>', _on_unmap)
        return
    try:
        root.mainloop()
    except Exception as e:
        log('Role window failed: %s' % e)
        logger.warning('Role window failed: %s', e)
        _OPEN['root'] = None
        _OPEN['refresh'] = None
