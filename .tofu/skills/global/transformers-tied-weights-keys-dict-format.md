---
name: transformers-tied-weights-keys-dict-format
description: Bug fix: transformers>=4.51 changed _tied_weights_keys from list to dict format {target: source}, causing AttributeError 'list object has no attribute keys' in get_expanded_tied_weights_keys
enabled: true
tags: [python, transformers, huggingface, bug-pattern, weight-tying]
created: 2026-03-26T07:06:00Z
updated: 2026-03-26T07:06:00Z
---

# transformers `_tied_weights_keys` format change (>=4.51)

## Bug Pattern
When upgrading transformers to >=4.51, models with `_tied_weights_keys` defined as a **list** will crash during `post_init()` with:

```
AttributeError: 'list' object has no attribute 'keys'
```

at `modeling_utils.py` → `get_expanded_tied_weights_keys()` → `tied_mapping.keys()`.

## Root Cause
The `_tied_weights_keys` format changed from a **list of strings** to a **dict** mapping `{target_weight: source_weight}`.

## Fix
```python
# OLD (list format, transformers < 4.51):
_tied_weights_keys = ["lm_head.weight"]

# NEW (dict format, transformers >= 4.51):
_tied_weights_keys = {"lm_head.weight": "model.embed_tokens.weight"}
```

The dict maps the **output weight** (target) to the **input embedding weight** (source) that it's tied to.

## How to find the correct names
- Check `get_input_embeddings()` → returns the embedding module → its `.weight` is the source
- Check `get_output_embeddings()` → returns the output projection → its `.weight` is the target
- Include the full parameter path from the model root (e.g., `model.embeddings.tok_embeddings.weight`)

