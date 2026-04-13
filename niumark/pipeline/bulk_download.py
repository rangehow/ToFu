#!/usr/bin/env python3
"""
Bulk PDF downloader for NiuMark training data.

Combines multiple sources for massive-scale PDF collection:

1. arXiv GCS Bucket (gs://arxiv-dataset) — FREE, 2.4M+ PDFs
   - Direct HTTP download, no API key needed
   - Organized by YYMM/arXiv_id 
   - ~25K papers/month, covers 2000-2026

2. Kaggle arXiv Metadata — 2.4M paper metadata (JSON)
   - Needed to get arXiv IDs + categories for targeted sampling
   - Download once, use to drive GCS downloads

3. FinePDFs URLs (HuggingFace) — 475M PDF URLs from Common Crawl
   - Non-academic PDFs: reports, manuals, books, government docs
   - Adds layout diversity beyond academic papers

Usage:
    # Download 100K recent arXiv PDFs (2023-2026), 50 parallel workers
    python -m pipeline.bulk_download --source arxiv --target 100000 --workers 50

    # Download 500K arXiv PDFs from all years
    python -m pipeline.bulk_download --source arxiv --target 500000 --workers 80 --years 2018-2026

    # Download from Common Crawl PDF URLs (diverse layouts)
    python -m pipeline.bulk_download --source commoncrawl --target 100000 --workers 30
    
    # Resume interrupted download
    python -m pipeline.bulk_download --source arxiv --target 100000 --resume
"""

import os
import sys
import json
import time
import random
import hashlib
import logging
import argparse
import urllib.request
import urllib.error
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger('bulk_download')


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
GCS_BASE = 'https://storage.googleapis.com/arxiv-dataset/arxiv/arxiv/pdf'
GCS_DL_PREFIX = 'https://storage.googleapis.com/arxiv-dataset'
GCS_LIST_API = 'https://storage.googleapis.com/storage/v1/b/arxiv-dataset/o'
ARXIV_PDF_URL = 'https://arxiv.org/pdf'

# arXiv YYMM ranges: 9108 (1991-08) through current
# Post-2007 format: YYMM.NNNNN
# We focus on recent years for quality + relevance


def _make_output_dir(base_dir: str) -> Path:
    p = Path(base_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Source 1: arXiv GCS Bucket — bulk listing + parallel download
# ---------------------------------------------------------------------------

def list_gcs_arxiv_ids(yymm: str, max_results: int = 50000) -> list:
    """List all arXiv PDF keys in a given YYMM prefix from GCS."""
    ids = []
    page_token = None
    
    while True:
        url = (f'{GCS_LIST_API}?prefix=arxiv/arxiv/pdf/{yymm}/'
               f'&maxResults=1000&fields=items(name,size),nextPageToken')
        if page_token:
            url += f'&pageToken={page_token}'
        
        try:
            resp = urllib.request.urlopen(url, timeout=30)
            data = json.loads(resp.read())
        except Exception as e:
            logger.warning('GCS list failed for %s: %s', yymm, e)
            break
        
        for item in data.get('items', []):
            name = item['name']  # arxiv/arxiv/pdf/2403/2403.00001v1.pdf
            arxiv_id = name.split('/')[-1].replace('.pdf', '')
            # Remove version suffix for dedup (keep v1 only for simplicity)
            base_id = arxiv_id.rsplit('v', 1)[0] if 'v' in arxiv_id else arxiv_id
            ids.append({
                'arxiv_id': base_id,
                'gcs_key': name,
                'size': int(item.get('size', 0)),
                'version': arxiv_id,
            })
        
        page_token = data.get('nextPageToken')
        if not page_token or len(ids) >= max_results:
            break
    
    # Deduplicate by base_id, keep highest version
    seen = {}
    for item in ids:
        bid = item['arxiv_id']
        if bid not in seen or item['version'] > seen[bid]['version']:
            seen[bid] = item
    
    return list(seen.values())


def download_one_pdf(item: dict, output_dir: Path, source: str = 'gcs') -> dict:
    """Download a single PDF. Returns result dict."""
    arxiv_id = item['arxiv_id']
    safe_name = arxiv_id.replace('/', '_')
    out_path = output_dir / f'{safe_name}.pdf'
    
    if out_path.exists() and out_path.stat().st_size > 1000:
        return {'arxiv_id': arxiv_id, 'status': 'exists', 'path': str(out_path)}
    
    if source == 'gcs':
        url = f'{GCS_DL_PREFIX}/{item["gcs_key"]}'
    else:
        url = f'{ARXIV_PDF_URL}/{arxiv_id}'
    
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'NiuMark-Bulk/1.0'})
            resp = urllib.request.urlopen(req, timeout=30)
            data = resp.read()
            
            if len(data) < 1000:
                return {'arxiv_id': arxiv_id, 'status': 'too_small', 'size': len(data)}
            
            # Write atomically
            tmp_path = out_path.with_suffix('.tmp')
            with open(tmp_path, 'wb') as f:
                f.write(data)
            tmp_path.rename(out_path)
            
            return {'arxiv_id': arxiv_id, 'status': 'ok', 'path': str(out_path), 'size': len(data)}
            
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {'arxiv_id': arxiv_id, 'status': 'not_found'}
            if e.code == 429:
                wait = (attempt + 1) * 30
                time.sleep(wait)
                continue
            logger.debug('HTTP %d for %s', e.code, arxiv_id)
        except Exception as e:
            if attempt < 2:
                time.sleep(5)
                continue
            logger.debug('Download failed %s: %s', arxiv_id, e)
    
    return {'arxiv_id': arxiv_id, 'status': 'failed'}


def bulk_download_arxiv_gcs(
    output_dir: str,
    target_count: int = 100000,
    workers: int = 50,
    year_range: str = '2023-2026',
    resume: bool = True,
):
    """Bulk download arXiv PDFs from the free GCS bucket."""
    out_path = _make_output_dir(output_dir)
    progress_file = out_path / '_progress.json'
    
    # Parse year range
    start_year, end_year = [int(y) for y in year_range.split('-')]
    
    # Generate YYMM prefixes
    yymm_list = []
    for year in range(start_year, end_year + 1):
        for month in range(1, 13):
            yymm = f'{year % 100:02d}{month:02d}'
            if year >= 2000:
                yymm = f'{year - 2000:02d}{month:02d}'
            # Skip future months
            if year == 2026 and month > 4:
                continue
            yymm_list.append(yymm)
    
    # Load progress
    progress = {}
    if resume and progress_file.exists():
        try:
            progress = json.loads(progress_file.read_text())
        except Exception:
            pass
    
    completed_yymm = set(progress.get('completed_yymm', []))
    total_downloaded = progress.get('total_downloaded', 0)
    
    # Count existing files
    existing = sum(1 for f in out_path.glob('*.pdf') if f.stat().st_size > 1000)
    if existing > 0:
        logger.info('Found %d existing PDFs in %s', existing, out_path)
        total_downloaded = max(total_downloaded, existing)
    
    if total_downloaded >= target_count:
        logger.info('Already have %d PDFs (target: %d). Done.', total_downloaded, target_count)
        return total_downloaded
    
    # Calculate papers per month to distribute evenly
    remaining_months = [y for y in yymm_list if y not in completed_yymm]
    if not remaining_months:
        logger.info('All months completed. Total: %d', total_downloaded)
        return total_downloaded
    
    papers_needed = target_count - total_downloaded
    per_month = max(100, papers_needed // len(remaining_months) + 1)
    
    logger.info('=== arXiv GCS Bulk Download ===')
    logger.info('Target: %d PDFs, have: %d, need: %d', target_count, total_downloaded, papers_needed)
    logger.info('Months to process: %d, ~%d papers/month', len(remaining_months), per_month)
    logger.info('Workers: %d', workers)
    
    # Shuffle months for diversity (don't go purely chronological)
    random.shuffle(remaining_months)
    
    for i, yymm in enumerate(remaining_months):
        if total_downloaded >= target_count:
            logger.info('🎯 Target reached: %d PDFs', total_downloaded)
            break
        
        logger.info('[%d/%d] Listing GCS prefix %s...', i + 1, len(remaining_months), yymm)
        
        try:
            items = list_gcs_arxiv_ids(yymm, max_results=per_month * 2)
        except Exception as e:
            logger.warning('Failed to list %s: %s', yymm, e)
            continue
        
        if not items:
            logger.info('  No items for %s', yymm)
            completed_yymm.add(yymm)
            continue
        
        # Sample if we have more than needed
        if len(items) > per_month:
            items = random.sample(items, per_month)
        
        logger.info('  Downloading %d PDFs from %s...', len(items), yymm)
        
        month_ok = 0
        month_skip = 0
        month_fail = 0
        
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(download_one_pdf, item, out_path, 'gcs'): item
                for item in items
            }
            
            for future in as_completed(futures):
                try:
                    result = future.result()
                    if result['status'] == 'ok':
                        month_ok += 1
                        total_downloaded += 1
                    elif result['status'] == 'exists':
                        month_skip += 1
                    else:
                        month_fail += 1
                except Exception as e:
                    month_fail += 1
                    logger.debug('Worker exception: %s', e)
        
        logger.info('  ✓ %s: +%d new, %d skipped, %d failed (total: %d)',
                     yymm, month_ok, month_skip, month_fail, total_downloaded)
        
        completed_yymm.add(yymm)
        
        # Save progress after each month
        progress = {
            'completed_yymm': list(completed_yymm),
            'total_downloaded': total_downloaded,
            'target': target_count,
            'last_update': time.strftime('%Y-%m-%d %H:%M:%S'),
        }
        progress_file.write_text(json.dumps(progress, indent=2))
    
    logger.info('=== Download complete: %d PDFs ===', total_downloaded)
    return total_downloaded


# ---------------------------------------------------------------------------
# Source 2: Common Crawl PDFs via FinePDFs URL list
# ---------------------------------------------------------------------------

def download_commoncrawl_pdfs(
    output_dir: str,
    target_count: int = 100000,
    workers: int = 30,
):
    """Download diverse PDFs from Common Crawl URLs.
    
    Uses the FinePDFs dataset's URL list for diverse non-academic PDFs.
    Falls back to direct Common Crawl index queries if HF dataset unavailable.
    """
    out_path = _make_output_dir(output_dir)
    
    logger.info('=== Common Crawl PDF Download ===')
    logger.info('This requires the HuggingFace datasets library.')
    
    try:
        from datasets import load_dataset
    except ImportError:
        logger.error('Install datasets: pip install datasets')
        return 0
    
    logger.info('Loading FinePDFs URL index from HuggingFace...')
    
    try:
        # FinePDFs has URLs with Common Crawl offsets
        ds = load_dataset('HuggingFaceFW/finepdfs', split='train', streaming=True)
    except Exception as e:
        logger.error('Failed to load FinePDFs dataset: %s', e)
        logger.info('Falling back to direct Common Crawl URLs...')
        return _download_cc_direct(out_path, target_count, workers)
    
    total = 0
    batch = []
    batch_size = workers * 2
    
    for row in ds:
        url = row.get('url', '')
        if not url or not url.startswith('http'):
            continue
        
        # Filter for actual PDFs
        if '.pdf' not in url.lower():
            continue
        
        doc_id = hashlib.md5(url.encode()).hexdigest()[:16]
        batch.append({'url': url, 'doc_id': doc_id})
        
        if len(batch) >= batch_size:
            results = _download_batch_urls(batch, out_path, workers)
            total += results
            batch = []
            
            if total >= target_count:
                break
            
            if total % 1000 == 0:
                logger.info('  Common Crawl progress: %d / %d', total, target_count)
    
    # Process remaining batch
    if batch and total < target_count:
        total += _download_batch_urls(batch, out_path, workers)
    
    logger.info('=== Common Crawl complete: %d PDFs ===', total)
    return total


def _download_batch_urls(batch: list, output_dir: Path, workers: int) -> int:
    """Download a batch of URLs in parallel."""
    ok = 0
    
    def _dl(item):
        out_path = output_dir / f'cc_{item["doc_id"]}.pdf'
        if out_path.exists():
            return 'exists'
        try:
            req = urllib.request.Request(item['url'], headers={
                'User-Agent': 'NiuMark-Research/1.0 (academic research)'
            })
            resp = urllib.request.urlopen(req, timeout=15)
            data = resp.read()
            if len(data) < 5000:
                return 'too_small'
            # Quick PDF check
            if not data[:5] == b'%PDF-':
                return 'not_pdf'
            with open(out_path, 'wb') as f:
                f.write(data)
            return 'ok'
        except Exception:
            return 'failed'
    
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for result in executor.map(_dl, batch):
            if result == 'ok':
                ok += 1
    
    return ok


def _download_cc_direct(output_dir: Path, target_count: int, workers: int) -> int:
    """Fallback: search Common Crawl index for PDF URLs directly."""
    logger.info('Direct Common Crawl index search not implemented yet.')
    logger.info('Install datasets and use FinePDFs: pip install datasets')
    return 0


# ---------------------------------------------------------------------------
# Source 3: Kaggle arXiv metadata for targeted sampling
# ---------------------------------------------------------------------------

def download_kaggle_metadata(output_dir: str) -> str:
    """Download the Kaggle arXiv metadata snapshot.
    
    Returns path to the downloaded JSON file.
    This file has metadata for ALL 2.4M+ arXiv papers, enabling
    targeted sampling by category, date, etc.
    """
    out_path = _make_output_dir(output_dir)
    meta_file = out_path / 'arxiv-metadata-oai-snapshot.json'
    
    if meta_file.exists():
        # Count lines to verify
        with open(meta_file) as f:
            n = sum(1 for _ in f)
        logger.info('Kaggle metadata already exists: %d papers', n)
        return str(meta_file)
    
    logger.info('Downloading arXiv metadata from Kaggle...')
    logger.info('This requires kaggle CLI: pip install kaggle')
    logger.info('And KAGGLE_USERNAME + KAGGLE_KEY env vars set.')
    
    try:
        import subprocess
        result = subprocess.run([
            'kaggle', 'datasets', 'download', '-d', 'Cornell-University/arxiv',
            '-p', str(out_path), '--unzip'
        ], capture_output=True, text=True, timeout=600)
        
        if result.returncode == 0 and meta_file.exists():
            logger.info('✓ Metadata downloaded successfully')
            return str(meta_file)
        else:
            logger.error('Kaggle download failed: %s', result.stderr)
    except FileNotFoundError:
        logger.warning('kaggle CLI not found. Install: pip install kaggle')
    except Exception as e:
        logger.error('Metadata download failed: %s', e)
    
    # Fallback: generate from OAI-PMH or use GCS listing
    logger.info('Falling back to GCS-based ID discovery (slower but no Kaggle needed)')
    return ''


def sample_from_metadata(
    metadata_file: str,
    target_count: int = 100000,
    categories: list = None,
    min_year: int = 2020,
) -> list:
    """Sample arXiv IDs from metadata file with balanced categories.
    
    Returns list of {'arxiv_id': ..., 'categories': ..., 'title': ...} dicts.
    """
    logger.info('Sampling %d papers from metadata (min_year=%d)...', target_count, min_year)
    
    by_category = defaultdict(list)
    
    with open(metadata_file) as f:
        for line in f:
            try:
                paper = json.loads(line)
            except json.JSONDecodeError:
                continue
            
            arxiv_id = paper.get('id', '')
            cats = paper.get('categories', '').split()
            
            # Filter by year
            # ID format: YYMM.NNNNN or archive/YYMMNNN
            try:
                if '/' in arxiv_id:
                    yymm = arxiv_id.split('/')[1][:4]
                else:
                    yymm = arxiv_id[:4]
                year = int(yymm[:2])
                if year < 50:
                    year += 2000
                else:
                    year += 1900
                if year < min_year:
                    continue
            except (ValueError, IndexError):
                continue
            
            # Filter by category if specified
            if categories:
                if not any(c in cats for c in categories):
                    continue
            
            primary_cat = cats[0] if cats else 'unknown'
            by_category[primary_cat].append({
                'arxiv_id': arxiv_id,
                'categories': cats,
                'title': paper.get('title', ''),
            })
    
    # Balanced sampling across categories
    all_cats = sorted(by_category.keys())
    per_cat = max(1, target_count // len(all_cats))
    
    sampled = []
    for cat in all_cats:
        papers = by_category[cat]
        n = min(per_cat, len(papers))
        sampled.extend(random.sample(papers, n))
    
    # If we need more, randomly fill from all
    if len(sampled) < target_count:
        all_papers = [p for papers in by_category.values() for p in papers]
        existing_ids = {p['arxiv_id'] for p in sampled}
        remaining = [p for p in all_papers if p['arxiv_id'] not in existing_ids]
        need = target_count - len(sampled)
        if remaining:
            sampled.extend(random.sample(remaining, min(need, len(remaining))))
    
    random.shuffle(sampled)
    logger.info('Sampled %d papers across %d categories', len(sampled), len(all_cats))
    return sampled[:target_count]


def download_sampled_papers(
    samples: list,
    output_dir: str,
    workers: int = 50,
) -> int:
    """Download PDFs for sampled papers from GCS."""
    out_path = _make_output_dir(output_dir)
    
    # Convert to GCS items
    items = []
    for s in samples:
        arxiv_id = s['arxiv_id']
        if '/' in arxiv_id:
            # Old format: hep-ph/0001001
            yymm = arxiv_id.split('/')[1][:4]
            version = arxiv_id.split('/')[1]
        else:
            yymm = arxiv_id[:4]
            version = arxiv_id
        
        items.append({
            'arxiv_id': arxiv_id,
            'gcs_key': f'arxiv/arxiv/pdf/{yymm}/{version}v1.pdf',
        })
    
    logger.info('Downloading %d sampled PDFs with %d workers...', len(items), workers)
    
    total_ok = 0
    batch_size = 500
    
    for batch_start in range(0, len(items), batch_size):
        batch = items[batch_start:batch_start + batch_size]
        
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(download_one_pdf, item, out_path, 'gcs'): item
                for item in batch
            }
            
            for future in as_completed(futures):
                try:
                    result = future.result()
                    if result['status'] in ('ok', 'exists'):
                        total_ok += 1
                except Exception:
                    pass
        
        logger.info('  Progress: %d / %d downloaded', total_ok, len(items))
    
    return total_ok


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Bulk PDF downloader for NiuMark')
    parser.add_argument('--source', choices=['arxiv', 'commoncrawl', 'kaggle', 'all'],
                        default='arxiv', help='Data source')
    parser.add_argument('--target', type=int, default=100000,
                        help='Target number of PDFs')
    parser.add_argument('--workers', type=int, default=50,
                        help='Number of parallel download workers')
    parser.add_argument('--output', default='data/bulk_pdfs',
                        help='Output directory')
    parser.add_argument('--years', default='2020-2026',
                        help='Year range for arXiv (e.g., 2020-2026)')
    parser.add_argument('--resume', action='store_true', default=True,
                        help='Resume interrupted download')
    parser.add_argument('--categories', nargs='+', default=None,
                        help='Filter by arXiv categories (for kaggle source)')
    
    args = parser.parse_args()
    
    t0 = time.time()
    total = 0
    
    if args.source in ('arxiv', 'all'):
        n = bulk_download_arxiv_gcs(
            output_dir=os.path.join(args.output, 'arxiv'),
            target_count=args.target if args.source == 'arxiv' else args.target // 2,
            workers=args.workers,
            year_range=args.years,
            resume=args.resume,
        )
        total += n
    
    if args.source in ('commoncrawl', 'all'):
        n = download_commoncrawl_pdfs(
            output_dir=os.path.join(args.output, 'commoncrawl'),
            target_count=args.target if args.source == 'commoncrawl' else args.target // 4,
            workers=min(args.workers, 30),  # Be gentle with CC
        )
        total += n
    
    if args.source == 'kaggle':
        meta = download_kaggle_metadata(os.path.join(args.output, 'metadata'))
        if meta:
            samples = sample_from_metadata(
                meta, args.target, args.categories, min_year=2020)
            total = download_sampled_papers(
                samples, os.path.join(args.output, 'arxiv'), args.workers)
    
    elapsed = time.time() - t0
    logger.info('='*60)
    logger.info('TOTAL: %d PDFs downloaded in %.1f minutes', total, elapsed / 60)
    logger.info('Rate: %.1f PDFs/minute', total / max(elapsed / 60, 0.1))
    logger.info('='*60)


if __name__ == '__main__':
    main()
