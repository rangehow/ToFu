---
name: gemini-image-gen-multiturn-format
description: FRIDAY Gemini image gen multi-turn requires Google-native role-based format with inlineData base64 (NOT image_url.uri or flat format) — verified 2026-03-30
enabled: true
tags: [friday, gemini, image-generation, multi-turn, api-format, inlineData, base64, role-based]
created: 2026-03-30T09:07:58Z
updated: 2026-03-30T09:07:58Z
---

# Gemini Image Gen Multi-turn via FRIDAY Proxy

## Key Discovery (2026-03-30)

The FRIDAY proxy at `aigc.sankuai.com` passes through to Google Vertex AI for Gemini image generation.
For multi-turn image editing, **only the Google-native role-based format with `inlineData` base64 works**.

### ❌ Formats that DON'T work (400 Bad Request):
- Flat format without roles (FRIDAY docs example is wrong for multi-turn)
- `image_url.uri` with S3 URLs
- `fileData.fileUri` 
- `image` string format

### ✅ Format that WORKS:
```json
{
  "contents": [
    {"role": "user",  "parts": [{"text": "draw a cat"}]},
    {"role": "model", "parts": [
      {"text": "Here is a cat."},
      {"inlineData": {"mimeType": "image/png", "data": "<BASE64>"}}
    ]},
    {"role": "user",  "parts": [{"text": "make it blue"}]}
  ],
  "generationConfig": {"responseModalities": ["Text", "Image"]}
}
```

## Architecture
- **Frontend** (`main.js`): Collects `image_url` (prefers `remote_image_url`) from `_igResult` metadata
- **Route** (`routes/upload.py`): `_resolve_history_images()` converts URLs → base64 (local disk read or HTTP download)
- **Backend** (`lib/image_gen.py`): `_build_multiturn_contents()` builds role-based `contents` array
- Response also returns `remote_image_url` (S3 URL) for faster resolution in next round

## Important Notes
- OpenAI image models (gpt-image-1.5) are single-turn only — history is ignored for them
- Each image is ~0.5-1.5MB base64, so multi-turn requests can be large
- The `_resolve_history_images()` function handles both local `/api/images/` paths and remote `https://` URLs

