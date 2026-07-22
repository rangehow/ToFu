"""jsdom regression: the get_conversation ("View Conversation") DIGEST card.

WHY
---
``_renderConvDigest`` in ``static/js/ui/tool_rounds.js`` is the HUMAN view of
the View-Conversation tool — the primary deliverable a user sees when the agent
opens a past conversation. It was previously a thin card (bare tool NAMES, a
short preview, no timestamps, first-40-messages only). This harness loads the
REAL shipped ``tool_rounds.js`` under jsdom and drives ``_renderConvDigest`` on
the exact structured digest ``build_conversation_digest`` now emits, asserting
the card renders:
  1. rich tool chips (name + primary arg), not bare names;
  2. a per-message EXPAND (<details>) when a longer ``full`` text is present;
  3. the conversation last-updated time + per-message timestamps;
  4. the head/tail OMISSION marker row for a long conversation.

NEUTER (in a COPY; shipped file byte-identical after): make the tool-chip
branch ignore the descriptor ``arg`` → the primary-argument text vanishes → the
"chips carry the arg" assertion fails, proving the arg render is load-bearing.

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os

import pytest

from tests._jsdom import JS_DIR, run_harness

pytestmark = pytest.mark.unit


_DIGEST_JSON = r"""{
  "convId": "convXYZ1234",
  "title": "Fix Flask CORS",
  "preset": "sonnet",
  "msgCount": 20,
  "createdAt": 1700000000000,
  "updatedAt": 1700000500000,
  "messages": [
    { "index": 1, "role": "user", "text": "short question", "ts": 1700000000000 },
    { "index": 2, "role": "assistant", "text": "a preview line",
      "full": "a preview line that is considerably longer than the preview so an expand affordance is meaningful",
      "ts": 1700000100000,
      "tools": [
        { "name": "read_files", "arg": "lib/foo.py", "status": "done" },
        { "name": "run_command", "arg": "git status", "status": "error" }
      ] },
    { "omitted": 12 },
    { "index": 20, "role": "assistant", "text": "final conclusion", "ts": 1700000500000 }
  ],
  "truncated": true,
  "omitted": 12
}"""


_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="chatInner"></div></body>',
  targets: [process.argv[4], process.argv[2]],
  globals: {
    _convRenderFingerprint: () => 0,
    conversations: [],
    activeConvId: null,
  },
});

function frag(html) {
  const d = document.createElement('div');
  d.innerHTML = html;
  return d;
}

if (typeof _renderConvDigest !== 'function') {
  console.log('FAIL entry_exposed _renderConvDigest missing');
  report();
  return;
}
check('entry_exposed', true);

const cd = JSON.parse(DIGEST_JSON_PH);
const html = _renderConvDigest(cd);
const d = frag(html);

// (0) card renders at all
check('card_root', !!d.querySelector('.ptool-convdigest'));

// (1) rich tool chips: name + primary arg (not a bare comma-joined name list)
check('tool_chip_present', !!d.querySelector('.ptool-convdigest-tool'));
check('tool_name_present', html.indexOf('read_files') !== -1 && html.indexOf('run_command') !== -1);
check('tool_arg_present', html.indexOf('lib/foo.py') !== -1 && html.indexOf('git status') !== -1);
// a failed-status round gets the failed cue class
check('tool_failed_class', !!d.querySelector('.ptool-convdigest-tool-failed'));

// (2) per-message expand for the message that carries a longer `full`
const det = d.querySelector('details.ptool-convdigest-expand');
check('expand_details_present', !!det);
check('expand_full_body', !!d.querySelector('.ptool-convdigest-full') &&
  html.indexOf('considerably longer') !== -1);

// (3) timestamps: conversation-level updated chip + a per-message time
check('conv_updated_time', !!d.querySelector('.ptool-convdigest-time'));
check('msg_time_present', !!d.querySelector('.ptool-convdigest-msgtime'));
// message index gutter
check('msg_index_present', !!d.querySelector('.ptool-convdigest-idx'));

// (4) head/tail omission marker row
check('omission_marker', !!d.querySelector('.ptool-convdigest-omitted'));

// (5) it kept the tail message (conclusion), not just the head
check('tail_kept', html.indexOf('final conclusion') !== -1);

report();
""".replace("DIGEST_JSON_PH", __import__("json").dumps(_DIGEST_JSON))


def test_conv_digest_render():
    run_harness(
        target_js=os.path.join(JS_DIR, "ui", "tool_rounds.js"),
        body_js=_BODY,
        extra_targets=[os.path.join(JS_DIR, "ui", "streaming_swarm_panel.js")],
        min_pass=13,
        label="conv digest render",
    )


# NEUTER body: asserts the OUTCOME of dropping the arg (arg text absent, tool
# name still present) so every emitted line is a PASS — run_harness treats any
# FAIL line as a hard error, so a NEUTER must express its expectation as PASSes.
_NEUTER_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body></body>',
  targets: [process.argv[4], process.argv[2]],
  globals: { _convRenderFingerprint: () => 0, conversations: [], activeConvId: null },
});
const cd = JSON.parse(DIGEST_JSON_PH);
const html = _renderConvDigest(cd);
// Under NEUTER the primary arg is dropped → its text must be GONE …
check('NC_arg_gone', html.indexOf('lib/foo.py') === -1 && html.indexOf('git status') === -1);
// … while the tool NAME still renders (only the arg branch was neutered).
check('NC_name_kept', html.indexOf('read_files') !== -1);
report();
""".replace("DIGEST_JSON_PH", __import__("json").dumps(_DIGEST_JSON))


def test_NC_tool_arg_is_load_bearing(tmp_path):
    """NEUTER: drop the tool descriptor `arg` from the chip → the primary-arg
    text vanishes (arg-absent PASSes) while the name stays. Shipped
    tool_rounds.js is byte-identical afterwards."""
    src = os.path.join(JS_DIR, "ui", "tool_rounds.js")
    with open(src, encoding="utf-8") as f:
        original = f.read()
    anchor = 'const arg = isObj ? (tl.arg || "") : "";'
    assert anchor in original, "tool-arg anchor not found in tool_rounds.js"
    patched = original.replace(anchor, 'const arg = "";  // NC', 1)
    assert patched != original, "NC patch did not apply"
    nc_path = tmp_path / "tool_rounds_nc.js"
    nc_path.write_text(patched, encoding="utf-8")
    try:
        out = run_harness(
            target_js=str(nc_path),
            body_js=_NEUTER_BODY,
            extra_targets=[os.path.join(JS_DIR, "ui", "streaming_swarm_panel.js")],
            min_pass=2,
            label="conv digest NEUTER",
        )
        assert "PASS NC_arg_gone" in out, out
        assert "PASS NC_name_kept" in out, out
    finally:
        with open(src, encoding="utf-8") as f:
            assert f.read() == original, "shipped tool_rounds.js must be byte-identical"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
