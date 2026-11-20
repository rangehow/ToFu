# Project toolchains: persistent, declared, automatic

Owner: `lib/project_mod/toolchains.py`. Applied by
`lib/project_mod/run_command.py::_get_cmd_env` next to the shared tool paths
(`process_environment.apply_tool_paths`), after `bind_runtime_base`.
Specifications: `tests/project_mod/test_project_toolchains.py` and
`test_verification_toolchains.py`.

Isolated `make test` / landing verification applies the **frozen checkout's**
registry in its canonical environment too. It never inherits the launcher's
PATH or production `TOFU_DATA_DIR`. The effective registered PATH/variables
participate in the evidence key, so changing relevant project toolchain
configuration invalidates earlier verification evidence. Previously only
interactive command children saw the registry; verification silently skipped
installed TeX. Neither sourcing an env.sh nor disabling those tests is needed.

## The problem this replaces

Toolchains installed ad hoc (TeX Live in `/tmp`, a conda env a Worker rebuilt
itself) disappear with the host or the task checkout, so every paper or analysis
run started with reinstalling tools or sourcing a project-local `env.sh` that no
other project had. The knowledge project's `infra/latex/env.sh` was exactly that
workaround: a persistent prefix, two PATH edits and two variables, repeated in
every Makefile and runbook.

## Contract

A project declares its toolchains once in **`tofu.toolchains.json` at its root**.
Commit the file: a Worker's isolated checkout carries it, so Workers get the
same PATH without knowing the project. Example (the knowledge project's
`texlive2026` + `poppler` is the first migration; install paths elided here):

```json
{
  "version": 1,
  "toolchains": {
    "texlive2026": {
      "description": "TeX Live 2026 for latexmk/pdflatex and matplotlib usetex",
      "location": "/mnt/persistent/toolchains/texlive2026",
      "path": ["bin/x86_64-linux"],
      "env": {"TEXBIN": "{location}/bin/x86_64-linux"},
      "provides": ["pdflatex", "latexmk", "dvipng", "tlmgr"],
      "install": "cd $(dirname \"$TOOLCHAIN_LOCATION\")/src/install-tl-*/ && perl ./install-tl --profile=$(dirname \"$TOOLCHAIN_LOCATION\")/src/tl.profile --location https://mirrors.ctan.org/systems/texlive/tlnet && \"$TOOLCHAIN_LOCATION/bin/x86_64-linux/tlmgr\" install latexmk ..."
    },
    "poppler": {
      "location": "/mnt/persistent/toolchains/poppler",
      "path_append": ["bin"],
      "env": {"POPPLER": "{location}/bin"},
      "provides": ["pdffonts", "pdftoppm"],
      "install": "conda create -y -p \"$TOOLCHAIN_LOCATION\" -c conda-forge --override-channels poppler"
    }
  }
}
```

- `location` — absolute prefix on **persistent storage**. A location under
  `/tmp`, `/var/tmp`, `/dev/shm` or the process temporary directory is refused:
  that is the incident this feature ends. `location` need not exist yet.
- `path` — directories under `location` **prepended** to the child's PATH, in
  declaration order across toolchains. `path_append` — directories appended
  *after* the inherited PATH, for prefixes whose bin directory also ships
  `openssl`, `kinit`, `bzip2`, `tput`, … which must never shadow the system
  ones (the poppler case). Both reject absolute entries and `..`.
- `env` — variables the commands need; `{location}` expands. `PATH`, `HOME`,
  `TMPDIR`, `VIRTUAL_ENV`, `PYTHONHOME` and Tofu's own runtime/toolchain markers
  are reserved, so a declaration cannot break the command isolation.
- `provides` — executable names a complete install has, reported by `status`.
- `install` — the shell recipe that recreates `location`. It runs with
  `sh -ec`, `$TOOLCHAIN_LOCATION` set. Run it through `run_command`
  (`python3 -m lib.project_mod.toolchains install <name>`, or run the recipe
  yourself) so approval, Stop and output bounds apply; nothing is installed at
  environment-build time or at startup.

Every command of the project — the lead's and every Worker's, from any working
directory inside the project — then resolves the declared programs with no
sourcing, and sees the declared variables. The project's own `.venv` still wins
for Python, because a virtualenv must select its interpreter; PATH order is
project `path` entries → shared tool paths → inherited PATH → `path_append`.

## Missing and broken toolchains

A toolchain whose `location` is absent is **skipped**, named in the child's
`TOFU_TOOLCHAINS_MISSING`, and its sibling `TOFU_TOOLCHAINS` lists the applied
ones: commands that do not need it still run. A command that then exits 127
(command not found) gets a result notice naming the missing toolchain and its
install recipe. Nothing is installed or downloaded automatically.

A malformed registry is **not** skipped: the command fails before starting with
`Error starting command: <file>: <reason> … Fix tofu.toolchains.json`. Silently
running a different binary than the project declared is worse than stopping.

`python3 -m lib.project_mod.toolchains --project <root> status` reports each
declared toolchain, whether its location exists, and where its `provides`
programs resolve inside its own directories (credential-free, read-only).

## Decisions

Problem: per-host `/tmp` installs and per-project `env.sh` scripts made builds
irreproducible across hosts and Worker checkouts.
Decision: one committed per-project registry, applied in the single environment
builder all command children share; persistent locations enforced; install as
an explicit, recorded recipe rather than an implicit side effect.
Alternatives rejected: sourcing `env.sh` automatically (opaque, unbounded shell,
no validation, and every caller must opt in); a global registry in the data root
(another project shares nothing, and a Worker checkout could not carry it);
auto-installing a missing toolchain at spawn (an unbounded side effect on every
command, and no approval/Stop boundary); refusing all commands while a
toolchain is missing (a paper project's TeX must not block its data scripts).

## First migration: the knowledge project

`knowledge/tofu.toolchains.json` declares `texlive2026` and `poppler` with the
same locations and PATH policy `infra/latex/env.sh` had. `TEXBIN`/`POPPLER` keep
their names because `paper/*/Makefile` passes them; new commands simply use
`latexmk`, `pdffonts`, … directly. The paper Makefiles' fallback
`$(LATEX_ENV)` defaults keep working for non-Tofu shells.

## Isolated slide font fixture

The root registry's `misans-test-fixture` entry exposes `TOFU_MISANS_FIXTURE`.
It contains only the three SHA-256-addressed MiSans OTF assets pinned by
`get_font('misans').sources`; files are read-only. Provision explicitly with
`python3 -m lib.project_mod.toolchains install misans-test-fixture`.
The helper uses the shared HTTP transport only during that explicit install.
For offline provisioning from an existing verified cache, use
`python3 -m tests.support.misans_fixture <persistent-location> --cached <misans-cache>`.
No test downloads or repairs the persistent fixture. Missing or corrupt assets
fail rather than disabling embedding checks.

`tests/slides/conftest.py` verifies all pins and copies bytes (never hardlinks)
into a disposable per-session cache, then redirects the font cache owner for
slide tests. Production cache paths are neither read nor written by tests.
This keeps real MiSans embedding/rendering checks without vendoring 19.5 MB
of licensed font data in Git or borrowing production runtime state. Registry
pins remain the single authority; a pin change requires explicit provisioning.

## Document TeX dependencies: additive, signed local-mirror install

The root `texlive2026` entry owns the existing persistent prefix. Its explicit
install action runs `scripts/install_document_tex_packages.py --location
"$TOOLCHAIN_LOCATION"`. It adds `latexmk natbib pgfplots geometry lipsum xetex
ctex` and their TeX Live dependencies. It does **not** update, downgrade, remove,
or reinstall already-installed packages. No apt/dpkg operation is needed.

The helper fetches CTAN metadata and missing archives through
`lib.http_transport.client.http_get`, stages them under the persistent sibling
`src/document-tlnet`, and invokes the existing prefix's `tlmgr` against that
local mirror with `--verify-repo=all`. Metadata signature and SHA-512 archive
checks remain enabled. The receipt there records retrieval time, resolved source,
database checksum, package revisions/checksums and the count of pre-existing
package records checked unchanged. Downloads occur only during explicit install,
not on test runs. An absent prefix is a visible bootstrap prerequisite, never
an excuse to install into the task's TMPDIR.

Portable use from a checkout with its declared Python dependencies:

```sh
# Set the registry's location to this host's existing persistent TeX Live prefix.
PYTHONPATH="$PWD" .venv/bin/python3 -m lib.project_mod.toolchains install texlive2026
# Or target that same persistent prefix explicitly:
PYTHONPATH="$PWD" .venv/bin/python3 scripts/install_document_tex_packages.py \
  --location /your/persistent/toolchains/texlive2026
PYTHONPATH="$PWD" .venv/bin/python3 -m pytest -q -rs \
  tests/paper/test_latex_layout.py tests/project_mod/test_document_tex_install.py
```

For a genuinely new host, bootstrap TeX Live once using the already-provisioned
persistent `src/install-tl-*` installer and `src/tl.profile` (with `TEXDIR` set to
the chosen persistent prefix), then run the additive action above. Installer
acquisition must use the shared network authority; a local profile/installer is
not recreated by the dependency helper. Do not run the old installer over an
existing prefix. `ctex` brings the redistributable Fandol fonts: the CJK PDF-height
regression selects `fontset=fandol` and checks actual Chinese PDF text. It does
not require or pretend to install proprietary Windows SimSun/KaiTi fonts.

The production launcher's model-free slide integration fixture also copies the
same verified MiSans assets to its own test cache. This covers isolated launcher
verification as well as tests under `tests/slides/`.

Measured provisioning footprint (2026-11-18): the three MiSans fixture files
occupy 19,530,468 bytes; the 51 staged CTAN package archives occupy 96,916,264
bytes, separate from installed TeX files. These persist across task checkouts;
no per-task duplicate installation is retained. Archive names are package-based
and reused after checksum validation; fixture names are registry SHA-256 pins.
