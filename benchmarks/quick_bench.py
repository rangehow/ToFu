#!/usr/bin/env python3
"""Quick parallel benchmark for candidate models.

Tests 3 translation + 3 vision samples per model.
All models run in parallel for maximum speed.
"""

import sys, os, json, time, re, base64, logging, requests
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from lib import LLM_API_KEYS, LLM_BASE_URL

API_URL = f'{LLM_BASE_URL}/chat/completions'
KEYS = LLM_API_KEYS[:2]
_NO_PROXY = {'no_proxy': '*'}

# ── Models ──
MODELS = {
    'glm-4-flash':           {'vision': False, 'in': 0.0,  'out': 0.0,  'temp': 0.7},
    'Doubao-1.5-lite-32k':   {'vision': False, 'in': 0.3,  'out': 0.6,  'temp': 0},
    'Doubao-Seed-1.6-flash': {'vision': True,  'in': 0.3,  'out': 3.0,  'temp': 0},
    'gpt-4.1-nano':          {'vision': True,  'in': 0.72, 'out': 2.88, 'temp': 0},
    'glm-4.6v':              {'vision': True,  'in': 1.0,  'out': 3.0,  'temp': 0.7},
    'Doubao-Seed-1.8':       {'vision': True,  'in': 0.8,  'out': 8.0,  'temp': 0},
    'deepseek-chat':         {'vision': False, 'in': 2.0,  'out': 3.0,  'temp': 0},
    'kimi-k2.5':             {'vision': True,  'in': 0.7,  'out': 21.0, 'temp': 1},
    'gpt-4.1-mini':          {'vision': True,  'in': 2.88, 'out': 11.52,'temp': 0},
    'glm-4.7':               {'vision': False, 'in': 3.0,  'out': 14.0, 'temp': 0.7},
    # baselines
    'qwen3.6-plus':          {'vision': True,  'in': 2.0,  'out': 12.0, 'temp': 0},
    'gemini-2.5-flash':      {'vision': True,  'in': 2.16, 'out': 18.0, 'temp': 0},
}

# ── Translation samples (3 diverse) ──
TRANS = [
    {
        'id': 't01', 'dir': 'zh→en', 'domain': 'news',
        'src': '量子计算利用量子比特的叠加态和纠缠态进行信息处理，理论上能够在某些特定问题上实现指数级的加速。',
        'ref': 'Quantum computing utilizes the superposition and entanglement states of qubits for information processing, and can theoretically achieve exponential speedup on certain specific problems.',
    },
    {
        'id': 't02', 'dir': 'zh→en', 'domain': 'poetry',
        'src': '月落乌啼霜满天，江枫渔火对愁眠。姑苏城外寒山寺，夜半钟声到客船。',
        'ref': 'The moon sets, crows cry in the frosty sky; by the river, maples and fishing lights face my sleepless sorrow. Outside Suzhou city, from Cold Mountain Temple, the midnight bell reaches the traveler\'s boat.',
    },
    {
        'id': 't03', 'dir': 'en→zh', 'domain': 'tech',
        'src': 'The rapid advancement of artificial intelligence has raised important ethical questions about privacy, autonomy, and the potential displacement of human workers across various industries.',
        'ref': '人工智能的快速发展引发了关于隐私、自主性以及各行各业人类工人可能被取代等重要伦理问题。',
    },
]


def call_api(model_id, messages, max_tokens=1024, timeout=45):
    """Single API call. Returns (content, usage, latency_ms) or raises."""
    cfg = MODELS[model_id]
    temp = cfg['temp']
    key = KEYS[hash(model_id) % len(KEYS)]

    body = {
        'model': model_id, 'messages': messages,
        'max_tokens': max_tokens, 'temperature': temp, 'stream': False,
    }
    lower = model_id.lower()
    if 'doubao' in lower or 'seed' in lower:
        body['thinking'] = {'type': 'disabled'}

    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {key}',
    }

    t0 = time.time()
    r = requests.post(API_URL, headers=headers, json=body,
                      timeout=(10, timeout), proxies=_NO_PROXY)
    lat = (time.time() - t0) * 1000

    if r.status_code != 200:
        raise Exception(f'HTTP {r.status_code}: {r.text[:200]}')

    data = r.json()
    choices = data.get('choices') or []
    if not choices:
        raise Exception(f'No choices')

    content = choices[0].get('message', {}).get('content', '')
    usage = data.get('usage', {})

    # Strip <think> tags
    if '<think>' in content:
        content = re.sub(r'<think>[\s\S]*?</think>\s*', '', content).strip()
        if '<think>' in content:
            content = content[:content.index('<think>')].strip()

    return content.strip(), usage, lat


def call_vision(model_id, prompt, img_path, max_tokens=512, timeout=45):
    """Vision API call."""
    with open(img_path, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode()
    messages = [{'role': 'user', 'content': [
        {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{b64}'}},
        {'type': 'text', 'text': prompt},
    ]}]
    return call_api(model_id, messages, max_tokens=max_tokens, timeout=timeout)


def bench_one_model(model_id):
    """Run all tests for a single model. Returns result dict."""
    cfg = MODELS[model_id]
    result = {
        'model': model_id,
        'vision_capable': cfg['vision'],
        'price_in': cfg['in'], 'price_out': cfg['out'],
        'translation': [], 'vision': [],
        'errors': [],
    }

    # ── Translation ──
    for t in TRANS:
        src_lang, tgt_lang = t['dir'].split('→')
        if src_lang == 'zh':
            prompt = f"Translate the following Chinese text to English. Output ONLY the translation.\n\n{t['src']}"
        else:
            prompt = f"Translate the following English text to Chinese. Output ONLY the translation.\n\n{t['src']}"

        # Thinking models need more tokens (reasoning_tokens count against max_tokens)
        tok_limit = 2048 if any(x in model_id.lower() for x in ('kimi', 'glm-4.7', 'qwen', 'gemini')) else 512
        timeout_s = 90 if any(x in model_id.lower() for x in ('kimi', 'glm-4.7', 'qwen')) else 45
        try:
            content, usage, lat = call_api(model_id, [{'role': 'user', 'content': prompt}],
                                           max_tokens=tok_limit, timeout=timeout_s)
            result['translation'].append({
                'id': t['id'], 'domain': t['domain'], 'dir': t['dir'],
                'output': content, 'ref': t['ref'],
                'latency_ms': round(lat, 1),
                'in_tok': usage.get('prompt_tokens', 0),
                'out_tok': usage.get('completion_tokens', 0),
            })
        except Exception as e:
            result['translation'].append({
                'id': t['id'], 'error': str(e)[:150],
                'latency_ms': 0, 'in_tok': 0, 'out_tok': 0,
            })
            result['errors'].append(f"trans/{t['id']}: {str(e)[:100]}")

    # ── Vision (LaTeX OCR) ──
    if cfg['vision']:
        latex_data = os.path.join(PROJECT_ROOT, 'benchmarks', 'data', 'latex_samples.json')
        with open(latex_data) as f:
            samples = json.load(f)
        samples = [s for s in samples if s.get('image_path')
                   and os.path.exists(os.path.join(PROJECT_ROOT, s['image_path']))][:3]

        prompt = ("This image contains a mathematical formula. "
                  "Convert it to LaTeX code. Output ONLY the raw LaTeX, "
                  "no wrappers, no code blocks.")

        for i, s in enumerate(samples):
            img = os.path.join(PROJECT_ROOT, s['image_path'])
            gt = re.sub(r'\\begin\{align\*?\}|\\end\{align\*?\}', '', s['formula']).strip()
            vis_timeout = 90 if any(x in model_id.lower() for x in ('kimi', 'glm-4.7', 'qwen')) else 45
            vis_tok = 2048 if any(x in model_id.lower() for x in ('kimi', 'glm-4.7', 'qwen', 'gemini')) else 512
            try:
                content, usage, lat = call_vision(model_id, prompt, img, max_tokens=vis_tok, timeout=vis_timeout)
                # Clean
                pred = re.sub(r'```(?:latex|tex)?\s*', '', content)
                pred = re.sub(r'```\s*$', '', pred)
                pred = re.sub(r'\\begin\{align\*?\}|\\end\{align\*?\}', '', pred)
                pred = re.sub(r'^\$+|\$+$', '', pred).strip()

                result['vision'].append({
                    'idx': i, 'pred': pred, 'gt': gt,
                    'latency_ms': round(lat, 1),
                    'in_tok': usage.get('prompt_tokens', 0),
                    'out_tok': usage.get('completion_tokens', 0),
                })
            except Exception as e:
                result['vision'].append({
                    'idx': i, 'error': str(e)[:150],
                    'latency_ms': 0, 'in_tok': 0, 'out_tok': 0,
                })
                result['errors'].append(f"vision/{i}: {str(e)[:100]}")

    return result


def edit_distance(a, b):
    if not a and not b: return 0.0
    if not a or not b: return 1.0
    m, n = len(a), len(b)
    dp = list(range(n+1))
    for i in range(1, m+1):
        prev = dp[0]; dp[0] = i
        for j in range(1, n+1):
            tmp = dp[j]
            dp[j] = prev if a[i-1] == b[j-1] else 1 + min(prev, dp[j], dp[j-1])
            prev = tmp
    return round(dp[n] / max(m, n), 4)


def compute_bleu(pred, ref):
    try:
        import sacrebleu
        return round(sacrebleu.sentence_bleu(pred, [ref]).score, 2)
    except Exception as exc:
        logger.debug('BLEU computation failed: %s', exc)
        return 0.0


def compute_char_bleu(pred, ref):
    try:
        import sacrebleu
        return round(sacrebleu.sentence_bleu(' '.join(list(pred)), [' '.join(list(ref))]).score, 2)
    except Exception as exc:
        logger.debug('Char-BLEU computation failed: %s', exc)
        return 0.0


# ═══════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════

def main():
    print(f'🚀 Quick Parallel Benchmark — {len(MODELS)} models × 3 trans + 3 vision')
    print(f'   Started: {datetime.now().strftime("%H:%M:%S")}')
    print()

    results = {}
    t_start = time.time()

    # Run all models in parallel (max 4 concurrent to avoid rate limits)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(bench_one_model, m): m for m in MODELS}
        for fut in as_completed(futures):
            model_id = futures[fut]
            try:
                r = fut.result()
                results[model_id] = r
                n_trans = sum(1 for t in r['translation'] if 'output' in t)
                n_vis = sum(1 for v in r['vision'] if 'pred' in v)
                n_err = len(r['errors'])
                print(f'  ✅ {model_id:<28} trans={n_trans}/3 vis={n_vis}/{"3" if r["vision_capable"] else "—"} err={n_err}')
            except Exception as e:
                print(f'  ❌ {model_id:<28} FAILED: {e}')

    elapsed = time.time() - t_start
    print(f'\n⏱  All models completed in {elapsed:.1f}s')

    # ── Compute metrics ──
    print('\n' + '='*120)
    print(f'  {"Model":<28} {"BLEU":>6} {"TrLat":>7} {"EditD":>7} {"VisLat":>7} '
          f'{"In¥/M":>6} {"Out¥/M":>7} {"TrTok":>7} {"VisTok":>7} {"Err":>4}')
    print('-'*120)

    model_scores = []

    for model_id in MODELS:
        r = results.get(model_id)
        if not r:
            continue

        cfg = MODELS[model_id]

        # Translation BLEU
        bleus = []
        t_lats = []
        t_in_tok = 0
        t_out_tok = 0
        for t in r['translation']:
            if 'output' in t:
                ref = t.get('ref', '')
                pred = t.get('output', '')
                d = t.get('dir', 'zh→en')
                if d.endswith('→zh'):
                    bleus.append(compute_char_bleu(pred, ref))
                else:
                    bleus.append(compute_bleu(pred, ref))
                t_lats.append(t['latency_ms'])
                t_in_tok += t['in_tok']
                t_out_tok += t['out_tok']

        avg_bleu = sum(bleus) / len(bleus) if bleus else 0
        avg_t_lat = sum(t_lats) / len(t_lats) if t_lats else 0

        # Vision edit distance
        edits = []
        v_lats = []
        v_in_tok = 0
        v_out_tok = 0
        for v in r['vision']:
            if 'pred' in v:
                edits.append(edit_distance(v['pred'], v['gt']))
                v_lats.append(v['latency_ms'])
                v_in_tok += v['in_tok']
                v_out_tok += v['out_tok']

        avg_edit = sum(edits) / len(edits) if edits else -1
        avg_v_lat = sum(v_lats) / len(v_lats) if v_lats else -1

        n_err = len(r['errors'])

        ed_str = f'{avg_edit:.4f}' if avg_edit >= 0 else '    —'
        vl_str = f'{avg_v_lat:>7.0f}' if avg_v_lat >= 0 else '      —'

        print(f'  {model_id:<28} {avg_bleu:>6.1f} {avg_t_lat:>7.0f} {ed_str:>7} {vl_str} '
              f'{cfg["in"]:>6.2f} {cfg["out"]:>7.2f} '
              f'{t_in_tok+t_out_tok:>7} {v_in_tok+v_out_tok:>7} {n_err:>4}')

        model_scores.append({
            'model': model_id,
            'avg_bleu': round(avg_bleu, 2),
            'avg_trans_lat': round(avg_t_lat, 0),
            'avg_edit_dist': round(avg_edit, 4) if avg_edit >= 0 else None,
            'avg_vis_lat': round(avg_v_lat, 0) if avg_v_lat >= 0 else None,
            'trans_tokens': t_in_tok + t_out_tok,
            'vis_tokens': v_in_tok + v_out_tok,
            'errors': n_err,
            'price_in': cfg['in'],
            'price_out': cfg['out'],
            'vision': cfg['vision'],
        })

    print('='*120)

    # ── LLM-as-Judge (use gemini-2.5-flash for judging) ──
    # Use a fast non-thinking model as judge (Doubao-1.5-lite is cheap + fast)
    judge_model = 'Doubao-1.5-lite-32k'
    print(f'\n🧑‍⚖️  Running LLM-as-Judge ({judge_model}) on translations...')

    # Batch all judgments
    all_judge_tasks = []
    for model_id in MODELS:
        r = results.get(model_id)
        if not r: continue
        for t in r['translation']:
            if 'output' not in t: continue
            all_judge_tasks.append((model_id, t))

    judge_scores = {}  # model_id → [scores]

    # Build source lookup table
    trans_src_map = {t['id']: t['src'] for t in TRANS}

    print(f'   ({len(all_judge_tasks)} translation judgments to make, serial to avoid rate limits...)')
    for mid, t in all_judge_tasks:
        src_text = trans_src_map.get(t.get('id'), t.get('output', ''))
        src_lang = t.get('dir', 'zh→en').split('→')[0]
        prompt = f"""Rate this translation 1-5 (5=perfect, 4=very good, 3=acceptable, 2=poor, 1=unusable).
Source ({src_lang}): {src_text}
Reference: {t['ref']}
Translation: {t['output']}
Output ONLY a single digit 1-5."""
        try:
            content, _, lat = call_api(judge_model, [{'role': 'user', 'content': prompt}],
                                       max_tokens=64, timeout=30)
            m = re.search(r'[1-5]', content)
            score = int(m.group()) if m else 0
            judge_scores.setdefault(mid, []).append(score)
            print(f'     {mid:<25} {t.get("id","?")}: judge={score} ({lat:.0f}ms) "{content[:20]}"')
        except Exception as e:
            judge_scores.setdefault(mid, []).append(0)
            print(f'     {mid:<25} {t.get("id","?")}: ERROR — {str(e)[:80]}')
        time.sleep(0.3)  # rate limit protection
    
    # Also judge vision
    print('🧑‍⚖️  Running LLM-as-Judge on LaTeX OCR...')
    vis_judge_scores = {}
    all_vis_tasks = []
    for model_id in MODELS:
        r = results.get(model_id)
        if not r: continue
        for v in r['vision']:
            if 'pred' not in v: continue
            all_vis_tasks.append((model_id, v))

    print(f'   ({len(all_vis_tasks)} vision judgments to make, serial...)')
    for mid, v in all_vis_tasks:
        prompt = f"""Rate LaTeX OCR accuracy 1-5 (5=exact match, 4=minor formatting diff, 3=1-2 errors, 2=partially correct, 1=wrong).
Ground truth: {v['gt']}
Prediction: {v['pred']}
Output ONLY a single digit 1-5."""
        try:
            content, _, lat = call_api(judge_model, [{'role': 'user', 'content': prompt}],
                                       max_tokens=64, timeout=30)
            m = re.search(r'[1-5]', content)
            score = int(m.group()) if m else 0
            vis_judge_scores.setdefault(mid, []).append(score)
            print(f'     {mid:<25} vis_{v.get("idx",0)}: judge={score} ({lat:.0f}ms) "{content[:20]}"')
        except Exception as e:
            vis_judge_scores.setdefault(mid, []).append(0)
            print(f'     {mid:<25} vis_{v.get("idx",0)}: ERROR — {str(e)[:80]}')
        time.sleep(0.3)

    # ── Final combined table ──
    print('\n' + '='*130)
    print('  🏆 FINAL RESULTS — Candidate Model Benchmark')
    print('='*130)
    print(f'  {"#":<3} {"Model":<28} {"BLEU":>6} {"TrJdg":>6} {"TrLat":>7} '
          f'{"VsJdg":>6} {"EditD":>7} {"VsLat":>7} '
          f'{"In¥":>5} {"Out¥":>6} {"Err":>4} {"Score":>6}')
    print('-'*130)

    for ms in model_scores:
        mid = ms['model']
        j_trans = judge_scores.get(mid, [])
        j_vis = vis_judge_scores.get(mid, [])
        avg_j_trans = sum(j_trans) / len(j_trans) if j_trans else 0
        avg_j_vis = sum(j_vis) / len(j_vis) if j_vis else 0
        ms['judge_trans'] = round(avg_j_trans, 2)
        ms['judge_vis'] = round(avg_j_vis, 2)

        # Composite score:
        # Translation quality (30%) + Vision quality (25%) + Speed (25%) + Cost (20%)
        t_quality = avg_j_trans / 5.0
        v_quality = avg_j_vis / 5.0 if ms['vision'] else 0
        speed = max(0, 1.0 - (ms['avg_trans_lat'] or 5000) / 5000)
        # Cost: normalize by max in group
        max_out = max(m['out'] for m in MODELS.values())
        cost_score = 1.0 - ms['price_out'] / max_out if max_out else 1.0

        if ms['vision']:
            composite = 0.30 * t_quality + 0.25 * v_quality + 0.25 * speed + 0.20 * cost_score
        else:
            composite = 0.45 * t_quality + 0.0 * v_quality + 0.30 * speed + 0.25 * cost_score
        ms['composite'] = round(composite, 4)

    # Sort by composite
    model_scores.sort(key=lambda x: x['composite'], reverse=True)

    for i, ms in enumerate(model_scores, 1):
        mid = ms['model']
        vis_j = f'{ms["judge_vis"]:.2f}' if ms['vision'] else '   —'
        ed = f'{ms["avg_edit_dist"]:.4f}' if ms.get('avg_edit_dist') is not None else '    —'
        vl = f'{ms["avg_vis_lat"]:>7.0f}' if ms.get('avg_vis_lat') is not None else '      —'

        print(f'  {i:<3} {mid:<28} {ms["avg_bleu"]:>6.1f} {ms["judge_trans"]:>6.2f} '
              f'{ms["avg_trans_lat"]:>7.0f} '
              f'{vis_j:>6} {ed:>7} {vl} '
              f'{ms["price_in"]:>5.2f} {ms["price_out"]:>6.2f} '
              f'{ms["errors"]:>4} {ms["composite"]:>6.3f}')

    print('='*130)

    # ── Save ──
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    out = PROJECT_ROOT / 'benchmarks' / f'candidate_bench_{ts}.json'
    with open(out, 'w', encoding='utf-8') as f:
        json.dump({
            'timestamp': datetime.now().isoformat(),
            'elapsed_s': round(elapsed, 1),
            'model_scores': model_scores,
            'raw_results': {k: v for k, v in results.items()},
            'judge_trans': judge_scores,
            'judge_vis': vis_judge_scores,
        }, f, ensure_ascii=False, indent=2)
    print(f'\n💾 Results saved: {out.name}')
    print(f'⏱  Total time: {time.time() - t_start:.1f}s')


if __name__ == '__main__':
    main()
