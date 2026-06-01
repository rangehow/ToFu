#!/usr/bin/env python3
"""Benchmark 10 Candidate Models — Translation + Vision (LaTeX OCR).

Tests each candidate model on:
  1. Translation (zh→en, en→zh): BLEU score + LLM-as-judge quality (1-5)
  2. Vision / LaTeX OCR: image → LaTeX accuracy (edit distance + LLM-as-judge)

Measures per model:
  - quality (BLEU, edit distance, judge score)
  - latency (TTFT, total)
  - cost (input/output tokens × pricing)
  - error rate

Usage:
    python benchmarks/candidate_models_bench.py [--quick] [--judge-model MODEL]

Requires server to NOT be running (uses lib directly).
"""

import sys, os, json, time, base64, re, argparse, logging, traceback
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Setup path ──
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-5s %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)

# ── Lazy imports (after path setup) ──
from lib import LLM_API_KEYS, LLM_BASE_URL

# ═══════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════

# 10 candidate models to benchmark
CANDIDATE_MODELS = [
    {
        'name': 'GLM-4-Flash',
        'model_id': 'glm-4-flash',
        'vision': False,
        'input_price_cny_per_m': 0.0,
        'output_price_cny_per_m': 0.0,
    },
    {
        'name': 'Doubao-1.5-lite-32k',
        'model_id': 'Doubao-1.5-lite-32k',
        'vision': False,
        'input_price_cny_per_m': 0.3,
        'output_price_cny_per_m': 0.6,
    },
    {
        'name': 'Doubao-Seed-1.6-flash',
        'model_id': 'Doubao-Seed-1.6-flash',
        'vision': True,
        'input_price_cny_per_m': 0.3,
        'output_price_cny_per_m': 3.0,
    },
    {
        'name': 'GPT-4.1-Nano',
        'model_id': 'gpt-4.1-nano',
        'vision': True,
        'input_price_cny_per_m': 0.72,
        'output_price_cny_per_m': 2.88,
    },
    {
        'name': 'GLM-4.6V',
        'model_id': 'glm-4.6v',
        'vision': True,
        'input_price_cny_per_m': 1.0,
        'output_price_cny_per_m': 3.0,
    },
    {
        'name': 'Doubao-Seed-1.8',
        'model_id': 'Doubao-Seed-1.8',
        'vision': True,
        'input_price_cny_per_m': 0.8,
        'output_price_cny_per_m': 8.0,
    },
    {
        'name': 'DeepSeek-Chat',
        'model_id': 'deepseek-chat',
        'vision': False,
        'input_price_cny_per_m': 2.0,
        'output_price_cny_per_m': 3.0,
    },
    {
        'name': 'Kimi-K2.5',
        'model_id': 'kimi-k2.5',
        'vision': True,
        'input_price_cny_per_m': 0.7,
        'output_price_cny_per_m': 21.0,
    },
    {
        'name': 'GPT-4.1-Mini',
        'model_id': 'gpt-4.1-mini',
        'vision': True,
        'input_price_cny_per_m': 2.88,
        'output_price_cny_per_m': 11.52,
    },
    {
        'name': 'GLM-4.7',
        'model_id': 'glm-4.7',
        'vision': False,
        'input_price_cny_per_m': 3.0,
        'output_price_cny_per_m': 14.0,
    },
]

# Existing models as baselines (pick 2-3 representative ones)
BASELINE_MODELS = [
    {
        'name': 'Qwen-3.6-Plus (baseline)',
        'model_id': 'qwen3.6-plus',
        'vision': True,
        'input_price_cny_per_m': 2.0,
        'output_price_cny_per_m': 12.0,
    },
    {
        'name': 'Gemini-2.5-Flash (baseline)',
        'model_id': 'gemini-2.5-flash',
        'vision': True,
        'input_price_cny_per_m': 2.16,
        'output_price_cny_per_m': 18.0,
    },
]

API_URL = f'{LLM_BASE_URL}/chat/completions'
API_KEYS = LLM_API_KEYS[:2]  # key0 and key1
_NO_PROXY = {'no_proxy': '*'}
_key_idx = 0  # round-robin key index


def _next_key():
    """Round-robin API key selection."""
    global _key_idx
    key = API_KEYS[_key_idx % len(API_KEYS)]
    _key_idx += 1
    return key


# ═══════════════════════════════════════════════════════════
#  Low-level API call (bypasses lib/llm/ for simplicity)
# ═══════════════════════════════════════════════════════════

def call_model(model_id, messages, *, max_tokens=2048, temperature=0,
               timeout=60):
    """Non-streaming chat completion. Returns (content, usage, latency_ms).
    
    Raises on error (no retries — we want raw signal).
    """
    import requests as req

    api_key = _next_key()
    # ── Model-specific temperature overrides ──
    lower = model_id.lower()
    if 'kimi' in lower:
        # Kimi-K2.5 requires temperature=1 (API returns 400 otherwise)
        temperature = 1
    elif 'glm' in lower:
        # GLM models work best with temperature=0.7
        temperature = max(temperature, 0.7)

    body = {
        'model': model_id,
        'messages': messages,
        'max_tokens': max_tokens,
        'temperature': temperature,
        'stream': False,
    }

    # Some models need specific params
    if 'doubao' in lower or 'seed' in lower:
        body['thinking'] = {'type': 'disabled'}
    
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}',
    }

    t0 = time.time()
    resp = req.post(API_URL, headers=headers, json=body,
                    timeout=(15, timeout), proxies=_NO_PROXY)
    latency_ms = (time.time() - t0) * 1000

    if resp.status_code != 200:
        raise Exception(f'HTTP {resp.status_code}: {resp.text[:300]}')

    data = resp.json()
    choices = data.get('choices') or []
    if not choices:
        raise Exception(f'No choices: {json.dumps(data)[:300]}')

    msg = choices[0].get('message', {})
    content = msg.get('content', '')
    usage = data.get('usage', {})

    # Strip <think> tags (DeepSeek, GLM, etc.)
    if content and '<think>' in content:
        content = re.sub(r'<think>[\s\S]*?</think>\s*', '', content).strip()
        if '<think>' in content:
            content = content[:content.index('<think>')].strip()

    return content, usage, latency_ms


def call_model_vision(model_id, text_prompt, image_path, *, max_tokens=1024,
                      temperature=0, timeout=60):
    """Vision API call with image. Returns (content, usage, latency_ms)."""
    # Read and encode image
    with open(image_path, 'rb') as f:
        img_bytes = f.read()
    b64 = base64.b64encode(img_bytes).decode('utf-8')
    
    # Determine media type
    ext = Path(image_path).suffix.lower()
    media_type = {'png': 'image/png', 'jpg': 'image/jpeg',
                  'jpeg': 'image/jpeg', 'webp': 'image/webp'}.get(ext, 'image/png')

    messages = [{
        'role': 'user',
        'content': [
            {
                'type': 'image_url',
                'image_url': {
                    'url': f'data:{media_type};base64,{b64}'
                }
            },
            {
                'type': 'text',
                'text': text_prompt,
            },
        ]
    }]

    return call_model(model_id, messages, max_tokens=max_tokens,
                      temperature=temperature, timeout=timeout)


# ═══════════════════════════════════════════════════════════
#  Translation Benchmark
# ═══════════════════════════════════════════════════════════

def load_translation_samples(path, max_samples=None):
    """Load translation samples."""
    with open(path, 'r', encoding='utf-8') as f:
        samples = json.load(f)
    if max_samples:
        samples = samples[:max_samples]
    return samples


def run_translation_bench(model, samples, verbose=True):
    """Run translation benchmark for a single model.
    
    Returns list of result dicts.
    """
    results = []
    model_id = model['model_id']
    model_name = model['name']

    for i, sample in enumerate(samples):
        src_lang = sample.get('source_lang', 'zh')
        tgt_lang = sample.get('target_lang', 'en')
        source = sample['source']
        reference = sample['reference']
        domain = sample.get('domain', 'general')
        
        if src_lang == 'zh':
            prompt = (
                f"Translate the following Chinese text to English. "
                f"Output ONLY the translation, nothing else.\n\n{source}"
            )
        else:
            prompt = (
                f"Translate the following English text to Chinese. "
                f"Output ONLY the translation, nothing else.\n\n{source}"
            )

        messages = [{'role': 'user', 'content': prompt}]
        
        try:
            content, usage, latency = call_model(model_id, messages,
                                                  max_tokens=1024, timeout=45)
            result = {
                'sample_id': sample['id'],
                'domain': domain,
                'direction': f'{src_lang}→{tgt_lang}',
                'translation': content.strip(),
                'reference': reference,
                'source': source,
                'latency_ms': round(latency, 1),
                'input_tokens': usage.get('prompt_tokens', 0),
                'output_tokens': usage.get('completion_tokens', 0),
                'error': None,
            }
            if verbose:
                preview = content.strip()[:60].replace('\n', ' ')
                logger.info(f'  [{model_name}] {sample["id"]} ({domain}): '
                           f'{latency:.0f}ms — "{preview}..."')
        except Exception as e:
            result = {
                'sample_id': sample['id'],
                'domain': domain,
                'direction': f'{src_lang}→{tgt_lang}',
                'translation': '',
                'reference': reference,
                'source': source,
                'latency_ms': 0,
                'input_tokens': 0,
                'output_tokens': 0,
                'error': str(e)[:200],
            }
            if verbose:
                logger.warning(f'  [{model_name}] {sample["id"]}: ERROR — {str(e)[:100]}')
        
        results.append(result)
        # Rate limit protection
        time.sleep(0.3)
    
    return results


# ═══════════════════════════════════════════════════════════
#  Vision / LaTeX OCR Benchmark
# ═══════════════════════════════════════════════════════════

def load_latex_samples(path, max_samples=None):
    """Load LaTeX formula samples."""
    with open(path, 'r') as f:
        samples = json.load(f)
    # Filter to those with actual images
    samples = [s for s in samples if s.get('image_path') and
               os.path.exists(os.path.join(PROJECT_ROOT, s['image_path']))]
    if max_samples:
        samples = samples[:max_samples]
    return samples


def run_vision_bench(model, samples, verbose=True):
    """Run vision/LaTeX OCR benchmark for a single model.
    
    Only runs for models with vision=True.
    Returns list of result dicts.
    """
    results = []
    model_id = model['model_id']
    model_name = model['name']

    if not model.get('vision'):
        if verbose:
            logger.info(f'  [{model_name}] Skipping vision bench (no vision capability)')
        return results

    prompt = (
        "This image contains a mathematical formula. "
        "Convert it to LaTeX code. Output ONLY the raw LaTeX code, "
        "do not include \\begin{align*} or \\end{align*} wrappers, "
        "and do not wrap in code blocks or markdown. Just the formula itself."
    )

    for i, sample in enumerate(samples):
        img_path = os.path.join(PROJECT_ROOT, sample['image_path'])
        gt_formula = sample['formula']
        
        # Strip align* wrapper from ground truth for comparison
        gt_clean = gt_formula
        gt_clean = re.sub(r'\\begin\{align\*?\}', '', gt_clean)
        gt_clean = re.sub(r'\\end\{align\*?\}', '', gt_clean)
        gt_clean = gt_clean.strip()

        try:
            content, usage, latency = call_model_vision(
                model_id, prompt, img_path, max_tokens=512, timeout=45)
            
            # Clean predicted formula
            pred = content.strip()
            # Remove common wrappers models add
            pred = re.sub(r'```(?:latex|tex)?\s*', '', pred)
            pred = re.sub(r'```\s*$', '', pred)
            pred = re.sub(r'\\begin\{align\*?\}', '', pred)
            pred = re.sub(r'\\end\{align\*?\}', '', pred)
            pred = re.sub(r'^\$+|\$+$', '', pred)
            pred = pred.strip()

            result = {
                'sample_idx': i,
                'prediction': pred,
                'ground_truth': gt_clean,
                'latency_ms': round(latency, 1),
                'input_tokens': usage.get('prompt_tokens', 0),
                'output_tokens': usage.get('completion_tokens', 0),
                'error': None,
            }
            if verbose:
                preview = pred[:50].replace('\n', ' ')
                logger.info(f'  [{model_name}] formula_{i:03d}: '
                           f'{latency:.0f}ms — "{preview}..."')
        except Exception as e:
            result = {
                'sample_idx': i,
                'prediction': '',
                'ground_truth': gt_clean,
                'latency_ms': 0,
                'input_tokens': 0,
                'output_tokens': 0,
                'error': str(e)[:200],
            }
            if verbose:
                logger.warning(f'  [{model_name}] formula_{i:03d}: ERROR — {str(e)[:100]}')

        results.append(result)
        time.sleep(0.3)
    
    return results


# ═══════════════════════════════════════════════════════════
#  Scoring — BLEU, Edit Distance, LLM-as-Judge
# ═══════════════════════════════════════════════════════════

def compute_bleu(prediction, reference):
    """Compute sentence-level BLEU using sacrebleu."""
    try:
        import sacrebleu
        bleu = sacrebleu.sentence_bleu(prediction, [reference])
        return round(bleu.score, 2)
    except Exception:
        return 0.0


def compute_char_bleu(prediction, reference):
    """Character-level BLEU for Chinese output."""
    try:
        import sacrebleu
        # Tokenize by characters for Chinese
        pred_chars = ' '.join(list(prediction))
        ref_chars = ' '.join(list(reference))
        bleu = sacrebleu.sentence_bleu(pred_chars, [ref_chars])
        return round(bleu.score, 2)
    except Exception:
        return 0.0


def compute_edit_distance(pred, gt):
    """Normalized character-level edit distance (0=perfect, 1=completely wrong)."""
    if not pred and not gt:
        return 0.0
    if not pred or not gt:
        return 1.0
    
    m, n = len(pred), len(gt)
    # Optimization: if lengths differ drastically, skip DP
    if abs(m - n) > max(m, n) * 0.8:
        return 1.0
    
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev = dp[0]
        dp[0] = i
        for j in range(1, n + 1):
            temp = dp[j]
            if pred[i-1] == gt[j-1]:
                dp[j] = prev
            else:
                dp[j] = 1 + min(prev, dp[j], dp[j-1])
            prev = temp
    
    return round(dp[n] / max(m, n), 4)


def run_llm_judge_translation(judge_model, translation_results, verbose=True):
    """Use an LLM to judge translation quality. Returns list of scores (1-5)."""
    scores = []
    
    for r in translation_results:
        if r.get('error') or not r.get('translation'):
            scores.append(0)
            continue

        prompt = f"""Rate the following translation on a scale of 1-5:
- 5: Perfect, publication-quality translation
- 4: Very good, minor stylistic issues only
- 3: Acceptable, conveys meaning but some awkwardness
- 2: Poor, significant errors or missing meaning
- 1: Unusable, major errors or wrong language

Source ({r['direction'].split('→')[0]}):
{r['source']}

Reference translation:
{r['reference']}

Model translation to judge:
{r['translation']}

Output ONLY a single digit (1-5), nothing else."""

        try:
            content, _, _ = call_model(judge_model, 
                                        [{'role': 'user', 'content': prompt}],
                                        max_tokens=8, timeout=20)
            score = int(re.search(r'[1-5]', content).group())
            scores.append(score)
        except Exception as e:
            if verbose:
                logger.warning(f'  Judge error: {e}')
            scores.append(0)
        time.sleep(0.2)
    
    return scores


def run_llm_judge_latex(judge_model, vision_results, verbose=True):
    """Use an LLM to judge LaTeX OCR quality. Returns list of scores (1-5)."""
    scores = []
    
    for r in vision_results:
        if r.get('error') or not r.get('prediction'):
            scores.append(0)
            continue

        prompt = f"""Rate how accurately this LaTeX OCR prediction matches the ground truth formula.
Score 1-5:
- 5: Exact or semantically identical match
- 4: Very close, minor whitespace/formatting differences only
- 3: Mostly correct, 1-2 symbol errors
- 2: Partially correct, captures structure but multiple errors
- 1: Mostly wrong

Ground truth:
{r['ground_truth']}

Prediction:
{r['prediction']}

Output ONLY a single digit (1-5), nothing else."""

        try:
            content, _, _ = call_model(judge_model,
                                        [{'role': 'user', 'content': prompt}],
                                        max_tokens=8, timeout=20)
            score = int(re.search(r'[1-5]', content).group())
            scores.append(score)
        except Exception as e:
            if verbose:
                logger.warning(f'  Judge error: {e}')
            scores.append(0)
        time.sleep(0.2)
    
    return scores


# ═══════════════════════════════════════════════════════════
#  Aggregate Results
# ═══════════════════════════════════════════════════════════

def aggregate_translation(model, results, judge_scores):
    """Compute aggregate metrics for translation results."""
    valid = [r for r in results if not r.get('error')]
    errors = [r for r in results if r.get('error')]
    
    if not valid:
        return {
            'model': model['name'],
            'model_id': model['model_id'],
            'task': 'translation',
            'n_total': len(results),
            'n_success': 0,
            'n_errors': len(errors),
            'avg_bleu': 0,
            'avg_judge_score': 0,
            'avg_latency_ms': 0,
            'total_input_tokens': 0,
            'total_output_tokens': 0,
            'est_cost_cny': 0,
        }

    # Compute BLEU scores
    bleu_scores = []
    for r in results:
        if r.get('error'):
            bleu_scores.append(0)
            continue
        if r['direction'].endswith('→zh'):
            # Chinese output — use char BLEU
            b = compute_char_bleu(r['translation'], r['reference'])
        else:
            b = compute_bleu(r['translation'], r['reference'])
        bleu_scores.append(b)
    
    total_in = sum(r['input_tokens'] for r in valid)
    total_out = sum(r['output_tokens'] for r in valid)
    cost = (total_in * model['input_price_cny_per_m'] +
            total_out * model['output_price_cny_per_m']) / 1_000_000

    valid_judges = [s for s in judge_scores if s > 0]
    
    return {
        'model': model['name'],
        'model_id': model['model_id'],
        'task': 'translation',
        'n_total': len(results),
        'n_success': len(valid),
        'n_errors': len(errors),
        'avg_bleu': round(sum(bleu_scores) / len(bleu_scores), 2) if bleu_scores else 0,
        'avg_judge_score': round(sum(valid_judges) / len(valid_judges), 2) if valid_judges else 0,
        'avg_latency_ms': round(sum(r['latency_ms'] for r in valid) / len(valid), 1),
        'p50_latency_ms': round(sorted(r['latency_ms'] for r in valid)[len(valid)//2], 1),
        'total_input_tokens': total_in,
        'total_output_tokens': total_out,
        'est_cost_cny': round(cost, 6),
        'bleu_scores': bleu_scores,
        'judge_scores': judge_scores,
        'error_details': [r['error'] for r in errors],
    }


def aggregate_vision(model, results, judge_scores):
    """Compute aggregate metrics for vision results."""
    valid = [r for r in results if not r.get('error')]
    errors = [r for r in results if r.get('error')]
    
    if not valid and not model.get('vision'):
        return {
            'model': model['name'],
            'model_id': model['model_id'],
            'task': 'vision_latex',
            'n_total': 0,
            'n_success': 0,
            'n_errors': 0,
            'skipped': True,
            'reason': 'No vision capability',
        }

    if not valid:
        return {
            'model': model['name'],
            'model_id': model['model_id'],
            'task': 'vision_latex',
            'n_total': len(results),
            'n_success': 0,
            'n_errors': len(errors),
            'avg_edit_dist': 1.0,
            'avg_judge_score': 0,
            'avg_latency_ms': 0,
            'est_cost_cny': 0,
        }

    # Compute edit distances
    edit_dists = []
    for r in results:
        if r.get('error'):
            edit_dists.append(1.0)
        else:
            ed = compute_edit_distance(r['prediction'], r['ground_truth'])
            edit_dists.append(ed)
    
    total_in = sum(r['input_tokens'] for r in valid)
    total_out = sum(r['output_tokens'] for r in valid)
    cost = (total_in * model['input_price_cny_per_m'] +
            total_out * model['output_price_cny_per_m']) / 1_000_000

    valid_judges = [s for s in judge_scores if s > 0]
    
    return {
        'model': model['name'],
        'model_id': model['model_id'],
        'task': 'vision_latex',
        'n_total': len(results),
        'n_success': len(valid),
        'n_errors': len(errors),
        'avg_edit_dist': round(sum(edit_dists) / len(edit_dists), 4) if edit_dists else 1.0,
        'avg_judge_score': round(sum(valid_judges) / len(valid_judges), 2) if valid_judges else 0,
        'avg_latency_ms': round(sum(r['latency_ms'] for r in valid) / len(valid), 1),
        'p50_latency_ms': round(sorted(r['latency_ms'] for r in valid)[len(valid)//2], 1) if valid else 0,
        'total_input_tokens': total_in,
        'total_output_tokens': total_out,
        'est_cost_cny': round(cost, 6),
        'edit_distances': edit_dists,
        'judge_scores': judge_scores,
        'error_details': [r['error'] for r in errors],
    }


# ═══════════════════════════════════════════════════════════
#  Pretty Print Results
# ═══════════════════════════════════════════════════════════

def print_translation_table(agg_results):
    """Print a nice table of translation results."""
    print('\n' + '='*110)
    print('  📝 TRANSLATION BENCHMARK RESULTS')
    print('='*110)
    print(f'{"Model":<28} {"BLEU":>6} {"Judge":>6} {"Lat(ms)":>8} {"P50":>8} '
          f'{"In Tok":>7} {"Out Tok":>8} {"Cost¥":>8} {"Err":>4}')
    print('-'*110)
    
    # Sort by judge score descending
    sorted_results = sorted(agg_results, key=lambda r: r.get('avg_judge_score', 0), reverse=True)
    
    for r in sorted_results:
        print(f'{r["model"]:<28} {r["avg_bleu"]:>6.1f} {r.get("avg_judge_score",0):>6.2f} '
              f'{r["avg_latency_ms"]:>8.0f} {r.get("p50_latency_ms",0):>8.0f} '
              f'{r["total_input_tokens"]:>7} {r["total_output_tokens"]:>8} '
              f'{r["est_cost_cny"]:>8.5f} {r["n_errors"]:>4}')
    print('='*110)


def print_vision_table(agg_results):
    """Print a nice table of vision results."""
    # Filter to non-skipped
    active = [r for r in agg_results if not r.get('skipped')]
    if not active:
        print('\n  👁 No vision results (no vision-capable models tested)')
        return
    
    print('\n' + '='*110)
    print('  👁 VISION / LaTeX OCR BENCHMARK RESULTS')
    print('='*110)
    print(f'{"Model":<28} {"EditDist":>8} {"Judge":>6} {"Lat(ms)":>8} {"P50":>8} '
          f'{"In Tok":>7} {"Out Tok":>8} {"Cost¥":>8} {"Err":>4}')
    print('-'*110)
    
    # Sort by judge score descending
    sorted_results = sorted(active, key=lambda r: r.get('avg_judge_score', 0), reverse=True)
    
    for r in sorted_results:
        print(f'{r["model"]:<28} {r["avg_edit_dist"]:>8.4f} {r.get("avg_judge_score",0):>6.2f} '
              f'{r["avg_latency_ms"]:>8.0f} {r.get("p50_latency_ms",0):>8.0f} '
              f'{r["total_input_tokens"]:>7} {r["total_output_tokens"]:>8} '
              f'{r["est_cost_cny"]:>8.5f} {r["n_errors"]:>4}')
    
    skipped = [r for r in agg_results if r.get('skipped')]
    if skipped:
        print('-'*110)
        for r in skipped:
            print(f'{r["model"]:<28} {"—— SKIPPED (text-only model) ——":>60}')
    print('='*110)


def print_combined_ranking(trans_aggs, vision_aggs):
    """Print final Pareto ranking combining both benchmarks."""
    print('\n' + '='*110)
    print('  🏆 COMBINED PARETO RANKING (Translation + Vision)')
    print('='*110)
    
    combined = {}
    for r in trans_aggs:
        name = r['model']
        combined[name] = {
            'model': name,
            'model_id': r['model_id'],
            'trans_bleu': r['avg_bleu'],
            'trans_judge': r.get('avg_judge_score', 0),
            'trans_latency': r['avg_latency_ms'],
            'trans_cost': r['est_cost_cny'],
            'trans_errors': r['n_errors'],
        }
    
    for r in vision_aggs:
        name = r['model']
        if name not in combined:
            combined[name] = {'model': name, 'model_id': r['model_id']}
        if r.get('skipped'):
            combined[name]['vision_edit_dist'] = '—'
            combined[name]['vision_judge'] = '—'
            combined[name]['vision_latency'] = '—'
            combined[name]['vision_cost'] = 0
        else:
            combined[name]['vision_edit_dist'] = r.get('avg_edit_dist', 1.0)
            combined[name]['vision_judge'] = r.get('avg_judge_score', 0)
            combined[name]['vision_latency'] = r.get('avg_latency_ms', 0)
            combined[name]['vision_cost'] = r.get('est_cost_cny', 0)
            combined[name]['vision_errors'] = r.get('n_errors', 0)
    
    # Compute composite score:
    # 40% translation judge + 20% vision judge + 20% speed (inverted) + 20% cost (inverted)
    for v in combined.values():
        t_score = v.get('trans_judge', 0) / 5.0  # normalize to 0-1
        v_score = (v.get('vision_judge', 0) / 5.0
                   if isinstance(v.get('vision_judge'), (int, float)) else 0)
        
        # Speed score: map 0-5000ms to 1.0-0.0
        t_lat = v.get('trans_latency', 5000)
        speed_score = max(0, 1.0 - t_lat / 5000)
        
        # Cost score: map 0-0.01 to 1.0-0.0  
        total_cost = v.get('trans_cost', 0) + (v.get('vision_cost', 0)
                                                if isinstance(v.get('vision_cost'), (int, float)) else 0)
        cost_score = max(0, 1.0 - total_cost / 0.01)
        
        has_vision = isinstance(v.get('vision_judge'), (int, float))
        if has_vision:
            v['composite'] = round(0.35 * t_score + 0.25 * v_score + 
                                    0.20 * speed_score + 0.20 * cost_score, 4)
        else:
            # Text-only: weight translation more
            v['composite'] = round(0.50 * t_score + 0.0 * v_score + 
                                    0.25 * speed_score + 0.25 * cost_score, 4)

    # Sort by composite score
    ranked = sorted(combined.values(), key=lambda x: x['composite'], reverse=True)
    
    print(f'{"#":<3} {"Model":<28} {"Trans":>5} {"Vis":>5} {"Spd":>5} '
          f'{"Cost":>6} {"Comp":>6} {"BL":>5} {"TL(ms)":>7} {"VL(ms)":>7}')
    print('-'*110)
    
    for i, r in enumerate(ranked, 1):
        vis_str = f'{r["vision_judge"]:.1f}' if isinstance(r.get('vision_judge'), (int, float)) else '—'
        vl_str = f'{r["vision_latency"]:.0f}' if isinstance(r.get('vision_latency'), (int, float)) else '—'
        print(f'{i:<3} {r["model"]:<28} '
              f'{r.get("trans_judge",0):>5.2f} {vis_str:>5} '
              f'{max(0, 1.0 - r.get("trans_latency", 5000) / 5000):>5.2f} '
              f'{r["composite"] - 0:>6.3f} {r["composite"]:>6.3f} '
              f'{r.get("trans_bleu",0):>5.1f} '
              f'{r.get("trans_latency",0):>7.0f} {vl_str:>7}')
    
    print('='*110)
    return ranked


# ═══════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='Benchmark candidate models')
    parser.add_argument('--quick', action='store_true',
                       help='Quick mode: fewer samples')
    parser.add_argument('--judge-model', default='gemini-2.5-flash',
                       help='Model to use as LLM judge (default: gemini-2.5-flash)')
    parser.add_argument('--no-baselines', action='store_true',
                       help='Skip baseline models')
    parser.add_argument('--no-judge', action='store_true',
                       help='Skip LLM-as-judge scoring')
    parser.add_argument('--models', nargs='+',
                       help='Only test these model IDs')
    args = parser.parse_args()

    logger.info('='*70)
    logger.info('  Candidate Model Benchmark — Translation + Vision')
    logger.info('='*70)
    logger.info(f'Judge model: {args.judge_model}')
    logger.info(f'Quick mode: {args.quick}')
    logger.info(f'API keys: {len(API_KEYS)} keys available')

    # Load data
    data_dir = PROJECT_ROOT / 'benchmarks' / 'data'
    
    trans_max = 6 if args.quick else 15
    vision_max = 5 if args.quick else 15
    
    trans_samples = load_translation_samples(data_dir / 'translation_samples.json', trans_max)
    latex_samples = load_latex_samples(data_dir / 'latex_samples.json', vision_max)
    
    logger.info(f'Loaded {len(trans_samples)} translation samples, {len(latex_samples)} LaTeX samples')

    # Build model list
    all_models = list(CANDIDATE_MODELS)
    if not args.no_baselines:
        all_models.extend(BASELINE_MODELS)
    
    if args.models:
        all_models = [m for m in all_models if m['model_id'] in args.models]
    
    logger.info(f'Testing {len(all_models)} models')

    # ── Phase 1: Translation ──
    logger.info('\n' + '─'*70)
    logger.info('  PHASE 1: Translation Benchmark')
    logger.info('─'*70)
    
    trans_raw = {}      # model_id → [results]
    trans_agg = []      # aggregated results
    
    for model in all_models:
        logger.info(f'\n📝 [{model["name"]}] Running translation...')
        results = run_translation_bench(model, trans_samples)
        trans_raw[model['model_id']] = results
        
        # LLM judge scoring
        if not args.no_judge:
            logger.info(f'  Judging translations with {args.judge_model}...')
            judge_scores = run_llm_judge_translation(args.judge_model, results)
        else:
            judge_scores = [0] * len(results)
        
        agg = aggregate_translation(model, results, judge_scores)
        trans_agg.append(agg)
        
        logger.info(f'  → BLEU={agg["avg_bleu"]:.1f}  Judge={agg.get("avg_judge_score",0):.2f}  '
                    f'Latency={agg["avg_latency_ms"]:.0f}ms  Errors={agg["n_errors"]}')
    
    print_translation_table(trans_agg)

    # ── Phase 2: Vision / LaTeX OCR ──
    logger.info('\n' + '─'*70)
    logger.info('  PHASE 2: Vision / LaTeX OCR Benchmark')
    logger.info('─'*70)
    
    vision_raw = {}
    vision_agg = []
    
    for model in all_models:
        logger.info(f'\n👁 [{model["name"]}] Running vision bench...')
        results = run_vision_bench(model, latex_samples)
        vision_raw[model['model_id']] = results
        
        if results and not args.no_judge:
            logger.info(f'  Judging LaTeX predictions with {args.judge_model}...')
            judge_scores = run_llm_judge_latex(args.judge_model, results)
        else:
            judge_scores = []
        
        agg = aggregate_vision(model, results, judge_scores)
        vision_agg.append(agg)
        
        if not agg.get('skipped'):
            logger.info(f'  → EditDist={agg["avg_edit_dist"]:.4f}  '
                       f'Judge={agg.get("avg_judge_score",0):.2f}  '
                       f'Latency={agg["avg_latency_ms"]:.0f}ms  Errors={agg["n_errors"]}')
    
    print_vision_table(vision_agg)

    # ── Phase 3: Combined Ranking ──
    ranked = print_combined_ranking(trans_agg, vision_agg)

    # ── Save Results ──
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_path = PROJECT_ROOT / 'benchmarks' / f'candidate_bench_{timestamp}.json'
    
    output = {
        'timestamp': datetime.now().isoformat(),
        'judge_model': args.judge_model,
        'quick_mode': args.quick,
        'n_translation_samples': len(trans_samples),
        'n_vision_samples': len(latex_samples),
        'translation_aggregated': trans_agg,
        'vision_aggregated': vision_agg,
        'combined_ranking': ranked,
        'translation_raw': {k: v for k, v in trans_raw.items()},
        'vision_raw': {k: v for k, v in vision_raw.items()},
    }
    
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    
    logger.info(f'\n✅ Results saved to {out_path}')
    logger.info(f'   Total models tested: {len(all_models)}')
    logger.info(f'   Translation samples: {len(trans_samples)}')
    logger.info(f'   Vision samples: {len(latex_samples)}')
    
    return output


if __name__ == '__main__':
    main()
