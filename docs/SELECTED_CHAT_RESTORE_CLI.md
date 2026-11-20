# Preview and apply passive chat history

Run from the destination Tofu installation, in its normal deployment environment.
Use an existing, trusted private storage connection file for the selected
external authority and the installation's existing local `data/` directory.
Do not copy the handoff file to another machine: it contains an authentication
secret. This command does not start a database, switch backends, or discover a
server. Embedded-only or otherwise unattachable authorities are not supported.

**SQL operator boundary:** Use only the private handoff supplied by a deployment
operator through the existing loopback-only storage connection contract. Before
preview or apply, independently verify which SQL deployment the handoff belongs
to and the numeric destination owner using trusted deployment and account
records. Its token authenticates a privileged storage operator, not the chosen
application user. `--owner` selects a destination; it supplies no user credential.
The local `data/` check matches CLI configuration only; it does not attest the SQL
endpoint or its volume. Native attachment retains its existing deployment,
engine, owner and tenant admission checks.

For a synthetic text-only archive:

```sh
.venv/bin/python3 -m lib.conv_ref.portable_restore_cli preview fixture.json \
    --owner 71 --connection-file "$HANDOFF" --data-directory "$DATA_DIRECTORY"
.venv/bin/python3 -m lib.conv_ref.portable_restore_cli apply fixture.json \
    --owner 71 --connection-file "$HANDOFF" --data-directory "$DATA_DIRECTORY" \
    --confirm "$PREVIEW_CONFIRMATION"
```

Copy the JSON `confirmation` from preview. It covers the archive bytes and the
reported target (including owner and the admitted host and port). The endpoint
is not a deployment identity. It is an operator guard, not
an authorization credential or a reservation. SQL preview encodes the command
with the existing storage frame encoder and omits confirmation if it cannot fit.
Native transport can carry payloads across frames; preview is not a guarantee of
transport or authority admission. Apply never splits a package into partial
writes. Preview validates the complete
archive including attachment hashes, then queries owned active headers. It
reports counts, unchanged imports, conflicts and tentative candidates. Trash,
foreign ownership, and changes after preview can still cause atomic apply to
refuse. Preview sends no mutation and prints no chat prose or original bytes.

**The CLI currently applies text-only archives.** It refuses archives with
originals rather than risking metadata pointing to bytes staged on a different
volume. The private SQL handoff identifies a database endpoint, not the
application's attachment volume. Use the independent [offline archive reader](SELECTED_CHAT_ARCHIVE_VIEW.md)
to view those packages or retrieve verified originals without import. Neither
CLI exports chats. No import grants execution, starts a provider or command,
or enables automatic Continue. Structural exclusion is not secret scanning;
review selected prose and files before sharing them.

Apply uses the atomic selected-authority import operation. On an unconfirmed
response, preview the same archive and target again before explicitly retrying.
Equal provenance is skipped; divergent matching identities are rejected.
