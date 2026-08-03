#!/usr/bin/env python3
"""desktop/role_window.py — the startup role window + control panel.

Owner directive 2026-08-03 (docs/DESKTOP_STARTUP_ROLE_UX_DESIGN.md): the
desktop app must SAY at startup whether this machine is the server or a
controlled endpoint, and the client controls must not live ONLY in the
system tray. This module is the ONE authoring consumed by BOTH launchers
(the connect_ui.py pattern — two copies of a role sentence would drift):

* **Full app** declares 「这台电脑是 Tofu 服务器」 and hosts the computer-
  control panel (enable toggle, permission tiers, connect-to-remote).
* **Agent app** declares 「这台电脑是 Tofu 受控端」 and hosts its four
  tiers, autostart and reconnect.

Deliberately split so headless CI can test every fact the window shows:

* **Pure builders** — :func:`role_state_full` / :func:`role_state_agent`
  return a plain dict of localized display facts. The tests assert the
  SENTENCES, never pixels.
* **The gate** — :func:`should_show_at_startup` reads ``show_role_window``
  from the agent config blob. Absent key (= fresh install AND upgrades
  from pre-window builds) means SHOW: nobody unchecks a window they have
  never seen.
* **The renderer** — :func:`show_role_window`, tkinter imported lazily
  (headless rule), themed by ``_tk_theme.apply_theme``, one re-entrant
  instance per process: a second call lifts the existing window instead
  of stacking duplicates.

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

    Merges into the existing config blob (agent_id, remote_server, …) —
    a clobbering write would erase the attachment the window itself just
    displayed. Failures are logged, never fatal.
    """
    try:
        from lib.desktop_agent.config import load_config, save_config
        cfg = load_config()
        cfg['show_role_window'] = bool(flag)
        save_config(cfg)
    except Exception as e:
        logger.warning('Could not persist show_role_window: %s', e)


# ═══════════════════════════════════════════════════════════════
#  Pure builders — every fact the window shows, headless-testable
# ═══════════════════════════════════════════════════════════════

def role_state_full(port, cc_state, attached_url, show_flag=True,
                    lang=None) -> dict:
    """Display facts for the FULL app's role window (the server side).

    ``dual_role`` is the tunnel-incident fact: a full app that is ALSO
    attached to a remote Tofu is both server and controlled endpoint, and
    the window must say so — that state was completely invisible when it
    last bit us.
    """
    from desktop import _tk_theme as theme
    lang = lang or theme.detect_lang()
    perms = (cc_state or {}).get('perms') or {}
    return {
        'kind': 'full',
        'lang': lang,
        'role': theme.t('desktop.role.serverTitle', lang),
        'server_url': 'http://127.0.0.1:%d' % port,
        'attached_url': attached_url or '',
        'dual_role': bool(attached_url),
        'cc_enabled': bool((cc_state or {}).get('enabled')),
        'perms': {k: bool(v) for k, v in perms.items()},
        'tiers': ['allow_write', 'allow_exec', 'allow_gui'],
        'show_at_startup': bool(show_flag),
    }


def role_state_agent(url, perms, autostart, show_flag=True,
                     lang=None) -> dict:
    """Display facts for the AGENT app's role window (the client side).

    ``autostart=None`` means the platform does not support the toggle
    (non-Windows v1) and the renderer hides the row — three-state, never
    a silent False.
    """
    from desktop import _tk_theme as theme
    lang = lang or theme.detect_lang()
    return {
        'kind': 'agent',
        'lang': lang,
        'role': theme.t('desktop.role.agentTitle', lang),
        'server_url': url or '',
        'attached': bool(url),
        'perms': {k: bool(v) for k, v in (perms or {}).items()},
        'tiers': ['allow_write', 'allow_exec', 'allow_gui', 'allow_egress'],
        'autostart': autostart,
        'show_at_startup': bool(show_flag),
    }


# ═══════════════════════════════════════════════════════════════
#  The renderer — lazy tkinter, one re-entrant instance
# ═══════════════════════════════════════════════════════════════

_OPEN = {'root': None}


def show_role_window(kind, state_fn, actions, log=_noop_log) -> None:
    """Show the role window modally; return when the user dismisses it.

    Args:
        kind: 'full' or 'agent' — selects the layout sections.
        state_fn: zero-arg callable returning a fresh builder dict on every
            refresh (the window never caches facts).
        actions: mutation callbacks. Shared keys: 'toggle_perm'(key),
            'connect'(). Full adds 'open'() and 'toggle_cc'(); agent adds
            'toggle_autostart'(). Every action is followed by a refresh.
        log: launcher-style diagnostic sink.

    Re-entrant: a second call while open lifts and refreshes the existing
    window — the tray's "Control panel…" item relies on this.
    """
    from desktop import _tk_theme as theme
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
            return
        except tk.TclError:
            _OPEN['root'] = None

    state = state_fn()
    lang = state.get('lang') or theme.detect_lang()

    root = tk.Tk()
    _OPEN['root'] = root
    palette = theme.apply_theme(root)
    root.title(state['role'])
    root.resizable(False, False)
    root.protocol('WM_DELETE_WINDOW', lambda: _close())

    frame = ttk.Frame(root, style='Tofu.TFrame', padding=20)
    frame.grid(sticky='nsew')

    header = ttk.Frame(frame, style='Tofu.TFrame')
    header.grid(row=0, column=0, sticky='w')
    photo = theme.load_logo_photo(root, size=40)
    if photo is not None:
        ttk.Label(header, image=photo, style='Tofu.TLabel').grid(
            row=0, column=0, padx=(0, 10))
    ttk.Label(header, text=state['role'],
              style='Tofu.Title.TLabel').grid(row=0, column=1, sticky='w')

    body = ttk.Frame(frame, style='Tofu.TFrame')
    body.grid(row=1, column=0, sticky='we', pady=(10, 0))

    show_var = tk.BooleanVar(value=bool(state.get('show_at_startup', True)))
    tier_vars = {}

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

    def _close():
        persist_show_at_startup(show_var.get())
        _OPEN['root'] = None
        try:
            root.destroy()
        except tk.TclError:
            pass

    def _tier_row(card, key, st, row):
        var = tk.BooleanVar(value=bool(st['perms'].get(key)))
        tier_vars[key] = var
        cb = ttk.Checkbutton(
            card, text=theme.t('desktop.tray.' + {
                'allow_write': 'permWrite', 'allow_exec': 'permExec',
                'allow_gui': 'permGui', 'allow_egress': 'permEgress',
            }[key], lang),
            style='Tofu.TCheckbutton', variable=var,
            command=lambda k=key: _act('toggle_perm', k))
        # The full app gates tiers behind the CC enable toggle (mirroring
        # the tray's _perm_enabled); the agent's tiers are always live.
        if st['kind'] == 'full' and not st['cc_enabled']:
            cb.state(['disabled'])
        cb.grid(row=row, column=0, sticky='w', pady=1)

    def _refresh():
        st = state_fn()
        for child in body.winfo_children():
            child.destroy()
        tier_vars.clear()
        row = 0

        sub_key = ('desktop.role.serverSub' if st['kind'] == 'full'
                   else 'desktop.role.agentSub')
        ttk.Label(body, wraplength=430, justify='left',
                  style='Tofu.Sub.TLabel',
                  text=theme.t(sub_key, st['lang'])).grid(
            row=row, column=0, sticky='w', pady=(0, 8))
        row += 1

        # ── Server card ──
        card = theme.card_frame(body, palette)
        card.grid(row=row, column=0, sticky='we', pady=(0, 8))
        row += 1
        url_text = st['server_url'] or theme.t('desktop.tray.notAttached',
                                               st['lang'])
        ttk.Label(card, text=url_text, style='CardName.TLabel').grid(
            row=0, column=0, sticky='w', padx=10, pady=(8, 2))
        if st['kind'] == 'full' and st.get('dual_role'):
            ttk.Label(card, wraplength=400, justify='left',
                      style='CardSub.TLabel',
                      text=theme.t('desktop.role.alsoClient', st['lang'])
                      .replace('{url}', st['attached_url'])).grid(
                row=1, column=0, sticky='w', padx=10, pady=(0, 4))
        btns = ttk.Frame(card, style='Card.TFrame')
        btns.grid(row=2, column=0, sticky='w', padx=10, pady=(2, 8))
        if st['kind'] == 'full':
            ttk.Button(btns, text=theme.t('desktop.tray.open', st['lang']),
                       style='Tofu.Accent.TButton',
                       command=lambda: _act('open')).grid(row=0, column=0,
                                                          padx=(0, 8))
        ttk.Button(btns,
                   text=theme.t('desktop.tray.connectRemote'
                                if st['kind'] == 'full'
                                else 'desktop.tray.connectDifferent',
                                st['lang']),
                   style='Tofu.TButton',
                   command=lambda: _act('connect')).grid(row=0, column=1)

        # ── Computer-control card ──
        cc = theme.card_frame(body, palette)
        cc.grid(row=row, column=0, sticky='we', pady=(0, 8))
        row += 1
        head = ttk.Frame(cc, style='Card.TFrame')
        head.grid(row=0, column=0, sticky='we', padx=10, pady=(8, 2))
        ttk.Label(head, text=theme.t('desktop.role.ccTitle', st['lang']),
                  style='CardName.TLabel').grid(row=0, column=0, sticky='w')
        if st['kind'] == 'full':
            status_key = ('desktop.role.ccOn' if st['cc_enabled']
                          else 'desktop.role.ccOff')
            style = ('Status.Ok.TLabel' if st['cc_enabled']
                     else 'CardSub.TLabel')
            ttk.Label(cc, wraplength=400, justify='left', style=style,
                      text=theme.t(status_key, st['lang'])).grid(
                row=1, column=0, sticky='w', padx=10)
            ttk.Button(head,
                       text=theme.t('desktop.role.disable'
                                    if st['cc_enabled']
                                    else 'desktop.role.enable', st['lang']),
                       style='Tofu.TButton',
                       command=lambda: _act('toggle_cc')).grid(
                row=0, column=1, sticky='e', padx=(20, 0))
        tier_row = 2
        for key in st['tiers']:
            _tier_row(cc, key, st, tier_row)
            tier_row += 1
        if st['kind'] == 'agent' and st.get('autostart') is not None:
            var = tk.BooleanVar(value=bool(st['autostart']))
            ttk.Checkbutton(
                cc, text=theme.t('desktop.tray.autostart', st['lang']),
                style='Tofu.TCheckbutton', variable=var,
                command=lambda: _act('toggle_autostart')).grid(
                row=tier_row, column=0, sticky='w', pady=1)
            tier_row += 1
        ttk.Label(cc, wraplength=400, justify='left', style='CardSub.TLabel',
                  text=theme.t('desktop.role.permHint', st['lang'])).grid(
            row=tier_row, column=0, sticky='w', padx=10, pady=(4, 8))

        # Sync the tier checkboxes with fresh state (a toggle may have
        # failed or been overridden by the config restore).
        for key, var in tier_vars.items():
            var.set(bool(st['perms'].get(key)))

    bottom = ttk.Frame(frame, style='Tofu.TFrame')
    bottom.grid(row=2, column=0, sticky='we', pady=(12, 0))
    ttk.Checkbutton(bottom,
                    text=theme.t('desktop.role.showAtStartup', lang),
                    style='Tofu.TCheckbutton',
                    variable=show_var).grid(row=0, column=0, sticky='w')
    ttk.Button(bottom, text=theme.t('desktop.role.minimize', lang),
               style='Tofu.Accent.TButton',
               command=_close).grid(row=0, column=1, sticky='e')
    bottom.columnconfigure(0, weight=1)

    _refresh()
    try:
        root.mainloop()
    except Exception as e:
        log('Role window failed: %s' % e)
        logger.warning('Role window failed: %s', e)
        _OPEN['root'] = None
