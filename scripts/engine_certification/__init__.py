"""One-command storage engine certification behind ``scripts/engine_certify.py``.

``gate.py`` runs the engine gate for one commit and publishes the write-once
record (read side: ``serverctl_pkg/engine_certification.py``). ``probes.py``,
``load_run.py`` and ``checkout_tasks.py`` run as child interpreters against the
checkout under test, so a historical commit is checked with its own code.
"""
