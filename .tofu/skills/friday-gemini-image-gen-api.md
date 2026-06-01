---
name: friday-gemini-image-gen-api
description: FRIDAY Gemini image gen: async Google-native API (submit+poll), imageSize/aspectRatio must be nested inside generationConfig.imageConfig (NOT top-level — causes 400), inlineData.data is S3 URL not base64, thought:true filter"
enabled: true
tags: [friday, gemini, image-generation, api, async, google-native, aigc, s3-url, base64, bug-pattern, imageSize, imageConfig, 400, aspectRatio]
created: 2026-03-28T14:23:49Z
updated: 2026-03-30T07:46:28Z
---

# FRIDAY Gemini Image Generation API

## API Endpoint (Google-native, NOT OpenAI)

Image generation for `gemini-*-image-*` models uses a **completely separate async API**:

### Step 1: Submit
```
POST https://aigc.sankuai.com/v1/google/models/{model}:imageGenerate
Authorization: Bearer {api_key}
Content-Type: application/json

{
  "contents": [{"parts": [{"text": "prompt here"}]}],
  "generationConfig": {
    "responseModalities": ["Text", "Image"],
    "imageConfig": {
      "imageSize": "1K",      // optional: 1K (default), 2K, 4K
      "aspectRatio": "16:9"   // optional: 1:1, 2:3, 3:2, 3:4, 4:3, 4:5, 5:4, 9:16, 16:9, 21:9
    }
  }
}
```
Returns: plain text task ID string (e.g. `"69c7e692-e4b0686c-d1310029-1774708370360"`)

### Step 2: Poll
```
GET https://aigc.sankuai.com/v1/google/models/{taskId}:imageGenerateQuery
Authorization: Bearer {api_key}
```
Returns:
- `{"status": 0, "data": "生成中"}` — still processing (poll every 3s)
- `{"status": 1, "data": {"candidates": [...]}}` — success
- `{"status": -1, "data": "error message"}` — failure

## ⚠️ Critical: `imageSize`/`aspectRatio` MUST be inside `imageConfig`

The Google Vertex AI `GenerationConfig` schema nests image controls inside an `imageConfig` object:

```
GenerationConfig
  ├── responseModalities: ["Text", "Image"]    ← top-level
  ├── imageConfig                               ← nested object!
  │     ├── imageSize: "1K" | "2K" | "4K"
  │     ├── aspectRatio: "1:1" | "16:9" | ...
  │     ├── personGeneration: "ALLOW_ALL" | ...
  │     └── imageOutputOptions: { mimeType, compressionQuality }
  └── ... (temperature, topP, etc.)
```

**Putting `imageSize` at the top level of `generationConfig`** causes the upstream Google API to reject the request with **400 Bad Request**. The FRIDAY proxy accepts the submit (returns task ID), but the async job fails.

Verified empirically (2026-03-30):
- ❌ `generationConfig: {responseModalities, imageSize: "1K"}` → 400
- ✅ `generationConfig: {responseModalities, imageConfig: {imageSize: "1K"}}` → success
- ✅ `generationConfig: {responseModalities, imageConfig: {imageSize: "2K", aspectRatio: "16:9"}}` → success
- ✅ `generationConfig: {responseModalities}` (no imageConfig) → success (defaults to 1K, 1:1)

Docs: https://docs.cloud.google.com/vertex-ai/generative-ai/docs/reference/rest/v1beta1/GenerationConfig#ImageConfig

## Critical Bug Pattern: inlineData.data is a URL, NOT base64!

The FRIDAY proxy returns the image as an **S3 URL** in `inlineData.data`, not inline base64:
```json
{"inlineData": {"mimeType": "image/png", "data": "https://s3plus.vip.sankuai.com/aigc-public-resources/gemini-image-generate/{taskId}.png"}}
```

**Fix**: detect URL prefix (`http://` or `https://`), download the image, then base64-encode it:
```python
if raw_data.startswith(('http://', 'https://')):
    img_resp = requests.get(raw_data, timeout=30)
    image_b64 = base64.b64encode(img_resp.content).decode('ascii')
```

## Thinking Parts Filter

Text parts with `"thought": true` are model reasoning and should be filtered out:
```python
if 'text' in part and part.get('thought'):
    continue  # skip thinking
```

## Supported Models (as of 2026-03)
- `gemini-3.1-flash-image-preview` (newest)
- `gemini-3-pro-image-preview`
- `gemini-2.5-flash-image`
- `gemini-2.5-flash-image-preview`
- `gemini-2.0-flash-preview-image-generation` (404 as of 2026-03-23 per community reports)

## Wrong APIs That DO NOT Work

| API | Error |
|-----|-------|
| `/v1/openai/native/chat/completions` + `modalities: ['text', 'image']` | HTTP 500 "bound must be positive" |
| `/v1/openai/native/chat/completions` + `response_modalities` | Same crash |
| `/v1/openai/native/images/generations` + gemini model name | Wrong API family |

## Typical Performance
- Submit: ~0.2s
- Generation: ~13-80s total
- Image size: 1376×768 PNG, ~2.7MB
- RPM: 10/min per key

