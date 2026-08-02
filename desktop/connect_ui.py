#!/usr/bin/env python3
"""desktop/connect_ui.py — the shared connect-line dialog + preseed import.

BOTH packaged components (the full desktop app and the agent-only build)
attach a machine to a Tofu server the SAME way: the user pastes one
connect line, or the installer baked a ``preseed_server.json`` next to
the exe. These two functions lived in ``desktop/launcher.py`` until the
agent build needed them too; they moved here so there is ONE authoring —
two copies of the dialog or the preseed contract would drift, and the
dialog's parser (``lib.desktop_agent.config.parse_connect_line``) is the
single owner of the wire format.

``desktop/launcher.py`` keeps thin delegating wrappers under its old
names — its call sites and test patch points are byte-identical.
"""

import os

from lib.log import get_logger

logger = get_logger(__name__)


def _noop_log(_msg: str) -> None:
    pass


def prompt_connect_line(current_url: str = '', log=_noop_log):
    """Ask for ONE pasted connect line; return (url, secret) or None.

    The web UI (Local Control → "This computer", remote case) renders a single
    click-to-copy line carrying BOTH the server address and the token. This
    dialog therefore takes ONE field: the user pastes what they copied and is
    done. Two separate fields would make them split the string by hand, which
    is the cognitive load the merged surface exists to remove.

    Parsing is delegated to lib.desktop_agent.config.parse_connect_line — the
    single owner of the format — so this dialog can never drift from what the
    web side emits. Returns None when the user cancels.
    """
    from lib.desktop_agent.config import parse_connect_line
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError as e:
        log('Connect dialog unavailable (no tkinter): %s' % e)
        logger.warning('Connect dialog unavailable (no tkinter): %s', e)
        return None
    from desktop import _tk_theme as theme

    lang = theme.detect_lang()
    result = {'value': None}
    root = tk.Tk()
    theme.apply_theme(root)
    root.title(theme.t('desktop.connect.title', lang))
    root.resizable(False, False)
    frame = ttk.Frame(root, style='Tofu.TFrame', padding=20)
    frame.grid(sticky='nsew')

    header = ttk.Frame(frame, style='Tofu.TFrame')
    header.grid(row=0, column=0, columnspan=2, sticky='w')
    photo = theme.load_logo_photo(root, size=40)
    if photo is not None:
        ttk.Label(header, image=photo, style='Tofu.TLabel').grid(
            row=0, column=0, padx=(0, 10))
    ttk.Label(header, text=theme.t('desktop.connect.heading', lang),
              style='Tofu.Title.TLabel').grid(row=0, column=1, sticky='w')
    ttk.Label(frame, wraplength=430, justify='left', style='Tofu.Sub.TLabel',
              text=theme.t('desktop.connect.instructions', lang)
              ).grid(row=1, column=0, columnspan=2, sticky='w', pady=(8, 12))

    entry = ttk.Entry(frame, width=58, style='Tofu.TEntry')
    entry.grid(row=2, column=0, columnspan=2, sticky='we')
    if current_url:
        ttk.Label(frame, style='Tofu.Sub.TLabel',
                  text=theme.t('desktop.connect.current', lang)
                  .replace('{url}', current_url)
                  ).grid(row=3, column=0, columnspan=2, sticky='w', pady=(8, 0))
    err = ttk.Label(frame, style='Tofu.Err.TLabel', wraplength=430,
                    justify='left')
    err.grid(row=4, column=0, columnspan=2, sticky='w', pady=(8, 0))

    def _ok(*_a):
        try:
            result['value'] = parse_connect_line(entry.get())
        except ValueError as ve:
            # Keep the dialog open with a specific reason — silently closing
            # would leave the user unable to tell what was wrong.
            err.config(text=str(ve))
            return
        root.destroy()

    def _cancel(*_a):
        result['value'] = None
        root.destroy()

    btns = ttk.Frame(frame, style='Tofu.TFrame')
    btns.grid(row=5, column=0, columnspan=2, sticky='e', pady=(14, 0))
    ttk.Button(btns, text=theme.t('desktop.connect.cancel', lang),
               style='Tofu.TButton', command=_cancel).grid(row=0, column=0,
                                                           padx=(0, 8))
    ttk.Button(btns, text=theme.t('desktop.connect.connect', lang),
               style='Tofu.Accent.TButton', command=_ok).grid(row=0, column=1)
    entry.bind('<Return>', _ok)
    root.bind('<Escape>', _cancel)
    entry.focus_set()

    try:
        root.mainloop()
    except Exception as e:
        log('Connect dialog failed: %s' % e)
        logger.warning('Connect dialog failed: %s', e)
        return None
    return result['value']


def import_preseed(exe_dir: str, log=_noop_log) -> None:
    """Import a server-baked ``preseed_server.json`` into the attachment.

    A server-built installer (lib/desktop_dist/winbuilder.py) bakes the
    address of the server it was built FROM next to the exe, so the first
    run attaches without the user pasting anything. Rules:

      * ONE-SHOT — the file is deleted after any attempt, so a stale
        preseed never overrides an attachment the user has since made.
      * NEVER overrides an existing attachment — the user's own connect
        wins over the install-time default.
      * NON-SECRET (the URL only) — the token still comes from the minted
        connect line or the tray dialog.
      * Any failure is logged and the file removed — a bad preseed must
        never wedge first run.
    """
    path = os.path.join(exe_dir, 'preseed_server.json')
    if not os.path.isfile(path):
        return
    try:
        import json
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        url = str(data.get('url') or '').strip()
        if not url.startswith(('http://', 'https://')):
            raise ValueError('preseed has no http(s) url')
        from lib.desktop_agent.config import remote_server, \
            save_remote_server
        existing, _secret = remote_server()
        if existing:
            log('Preseed ignored (already attached to %s)' % existing)
        else:
            save_remote_server(url, '')
            log('Preseeded remote attachment from installer: %s' % url)
    except Exception as e:
        log('Preseed import failed (ignored): %s' % e)
        logger.warning('Preseed import failed (ignored): %s', e)
    try:
        os.remove(path)
    except OSError as e:
        log('Could not remove preseed file: %s' % e)
        logger.debug('Could not remove preseed file %s: %s', path, e)
