#!/usr/bin/env python3
"""desktop/connect_ui.py — the "Join a server" dialog.

One field: the owner pastes the pairing link the server's Local Control panel
shows (or, for an already-paired legacy setup, an old ``<url> <token>`` connect
line). Parsing and the pair exchange belong to
:mod:`lib.desktop_wire.node_protocol` and :mod:`lib.desktop_agent.attachments`;
this module only renders, calls ``submit`` and shows its coded refusal.
"""

from lib.log import get_logger

logger = get_logger(__name__)


def _noop_log(_msg: str) -> None:
    pass


def prompt_join(submit, log=_noop_log):
    """Show the join dialog; returns the joined attachment, or None.

    ``submit(text, stop)`` performs the join on a worker thread and returns
    the attachment record, or raises an exception carrying a ``code``
    (``PairingInputError`` / ``JoinError``); the dialog then stays open with
    the localized reason. Joining can wait for the owner's browser to relay
    the pairing, so the dialog stays responsive and Cancel sets ``stop``.
    """
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError as e:
        log('Join dialog unavailable (no tkinter): %s' % e)
        logger.warning('Join dialog unavailable (no tkinter): %s', e)
        return None
    from desktop import _tk_theme as theme
    from desktop import _tk_host as host

    lang = theme.detect_lang()
    result = {'value': None}
    parent = host.parent_or_none()
    root = tk.Toplevel(parent) if parent is not None else tk.Tk()
    theme.apply_theme(root)
    root.title(theme.t('desktop.join.title', lang))
    root.resizable(False, False)
    theme.set_window_icon(root)
    frame = ttk.Frame(root, style='Tofu.TFrame', padding=20)
    frame.grid(sticky='nsew')

    header = ttk.Frame(frame, style='Tofu.TFrame')
    header.grid(row=0, column=0, columnspan=2, sticky='w')
    photo = theme.load_logo_photo(root, size=40)
    if photo is not None:
        ttk.Label(header, image=photo, style='Tofu.TLabel').grid(
            row=0, column=0, padx=(0, 10))
    ttk.Label(header, text=theme.t('desktop.join.heading', lang),
              style='Tofu.Title.TLabel').grid(row=0, column=1, sticky='w')
    ttk.Label(frame, wraplength=430, justify='left', style='Tofu.Sub.TLabel',
              text=theme.t('desktop.join.instructions', lang)
              ).grid(row=1, column=0, columnspan=2, sticky='w', pady=(8, 12))

    entry = ttk.Entry(frame, width=58, style='Tofu.TEntry')
    entry.grid(row=2, column=0, columnspan=2, sticky='we')
    err = ttk.Label(frame, style='Tofu.Err.TLabel', wraplength=430,
                    justify='left')
    err.grid(row=4, column=0, columnspan=2, sticky='w', pady=(8, 0))

    import threading
    stop = threading.Event()
    worker = {'thread': None, 'outcome': None}

    def _run(text):
        try:
            worker['outcome'] = ('ok', submit(text, stop))
        except Exception as exc:
            worker['outcome'] = ('error', exc)

    def _check():
        thread = worker['thread']
        if thread is None:
            return
        if thread.is_alive():
            root.after(200, _check)
            return
        worker['thread'] = None
        kind, value = worker['outcome']
        if stop.is_set():
            return
        if kind == 'error':
            err.config(text=theme.join_error_text(value, lang))
            return
        result['value'] = value
        root.destroy()

    def _ok(*_a):
        if worker['thread'] is not None:
            return
        err.config(text=theme.t('desktop.join.joining', lang))
        thread = threading.Thread(target=_run, args=(entry.get(),), daemon=True,
                                  name='tofu-node-join')
        worker['thread'] = thread
        thread.start()
        root.after(200, _check)

    def _cancel(*_a):
        stop.set()
        result['value'] = None
        root.destroy()

    btns = ttk.Frame(frame, style='Tofu.TFrame')
    btns.grid(row=5, column=0, columnspan=2, sticky='e', pady=(14, 0))
    ttk.Button(btns, text=theme.t('desktop.join.cancel', lang),
               style='Tofu.TButton', command=_cancel).grid(row=0, column=0,
                                                           padx=(0, 8))
    ttk.Button(btns, text=theme.t('desktop.join.join', lang),
               style='Tofu.Accent.TButton', command=_ok).grid(row=0, column=1)
    entry.bind('<Return>', _ok)
    root.bind('<Escape>', _cancel)
    entry.focus_set()
    theme.center_on_screen(root, width=520)

    if parent is not None:
        root.transient(parent)
        root.grab_set()
        try:
            parent.wait_window(root)
        except tk.TclError:
            pass
        return result['value']
    try:
        root.mainloop()
    except Exception as e:
        log('Join dialog failed: %s' % e)
        logger.warning('Join dialog failed: %s', e)
        return None
    return result['value']
