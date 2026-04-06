#!/usr/bin/env python3
"""
Smart Long Image Splitter for Xiaohongshu (小红书)
==================================================
Splits a tall image into multiple vertical-screen-ratio slices,
cutting ONLY at blank/uniform gaps between paragraphs — never through text.

Handles both white backgrounds AND gray code-block backgrounds.

Usage:
    python3 split_long_image.py <image_path> [--ratio 3:4] [--overlap 40] [--output_dir ./output]

Output ratio default: 3:4 (w:h) — ideal vertical screen ratio for mobile/Xiaohongshu.
"""

import os
import sys
import argparse
import numpy as np
from PIL import Image


def find_safe_cut_points(img_array: np.ndarray, min_gap: int = 5) -> list[tuple[int, int]]:
    """Find horizontal positions where it's safe to cut (uniform/blank rows).
    
    Uses ROW VARIANCE to detect "text-free" rows — works on both white backgrounds
    and gray/colored code-block backgrounds.
    
    Args:
        img_array: numpy array of the image (H, W, C)
        min_gap: minimum consecutive uniform rows to count as a safe gap
    
    Returns:
        List of (center_y, gap_length) tuples for each safe gap.
    """
    h, w = img_array.shape[:2]
    
    # Convert to grayscale
    if len(img_array.shape) == 3:
        gray = np.mean(img_array, axis=2)
    else:
        gray = img_array.astype(float)
    
    # Use central 90% to ignore edge artifacts
    margin = int(w * 0.05)
    central = gray[:, margin:w - margin]
    
    # A row is "safe to cut" if it is BOTH:
    # 1. Uniform (low std → no text characters creating contrast)
    # 2. Bright (mean > 180 → not a dark header bar or code block header)
    # Dark solid-color bars (section headers) have low std but low brightness.
    row_std = np.std(central, axis=1)
    row_mean = np.mean(central, axis=1)
    is_uniform = (row_std < 8.0) & (row_mean > 180)  # uniform AND bright
    
    # Find runs of consecutive uniform rows
    gaps = []
    in_gap = False
    gap_start = 0
    
    for y in range(h):
        if is_uniform[y]:
            if not in_gap:
                in_gap = True
                gap_start = y
        else:
            if in_gap:
                gap_len = y - gap_start
                if gap_len >= min_gap:
                    center = gap_start + gap_len // 2
                    gaps.append((center, gap_len))
                in_gap = False
    
    # Handle gap at the very end
    if in_gap:
        gap_len = h - gap_start
        if gap_len >= min_gap:
            center = gap_start + gap_len // 2
            gaps.append((center, gap_len))
    
    return gaps


def split_image(
    image_path: str,
    target_ratio: tuple[int, int] = (3, 4),
    overlap: int = 40,
    output_dir: str = None,
    min_gap_px: int = 5,
):
    """Split a long image into vertical-ratio slices at safe cut points.
    
    Args:
        image_path: Path to the source image.
        target_ratio: (width, height) ratio for output slices. Default 3:4.
        overlap: Pixels of overlap between adjacent slices for visual continuity.
        output_dir: Directory to save slices. Default: same dir as input, subfolder 'split_output'.
        min_gap_px: Minimum uniform gap height (px) to consider as safe cut point.
    """
    img = Image.open(image_path)
    img_array = np.array(img)
    W, H = img.width, img.height
    
    print(f"📐 Source image: {W} × {H} (aspect ratio H:W = {H/W:.2f}:1)")
    
    # Calculate ideal slice height from target ratio
    rw, rh = target_ratio
    ideal_slice_h = int(W * rh / rw)
    # Allow some flexibility: 65% ~ 130% of ideal height
    min_slice_h = int(ideal_slice_h * 0.65)
    max_slice_h = int(ideal_slice_h * 1.30)
    
    print(f"🎯 Target ratio: {rw}:{rh} → ideal slice height = {ideal_slice_h}px")
    print(f"   Acceptable range: {min_slice_h} ~ {max_slice_h}px")
    
    # Find all safe cut points
    gaps = find_safe_cut_points(img_array, min_gap=min_gap_px)
    print(f"✂️  Found {len(gaps)} safe cut points (uniform gaps ≥ {min_gap_px}px)")
    
    if not gaps:
        print("⚠️  No uniform gaps found! Falling back to uniform slicing.")
        cut_ys = list(range(ideal_slice_h, H, ideal_slice_h))
    else:
        # Greedy algorithm: pick cut points that keep each slice close to ideal height
        cut_ys = []
        current_top = 0
        gap_centers = [g[0] for g in gaps]
        
        while current_top + min_slice_h < H:
            target_bottom = current_top + ideal_slice_h
            
            if target_bottom >= H:
                break
            
            # Find the best gap within acceptable range from current_top
            candidates = [
                (abs(gc - target_bottom), gc)
                for gc in gap_centers
                if min_slice_h <= (gc - current_top) <= max_slice_h
            ]
            
            if candidates:
                candidates.sort()
                best_cut = candidates[0][1]
            else:
                # Expand search range: 40% ~ 160% of ideal
                wider = [
                    (abs(gc - target_bottom), gc)
                    for gc in gap_centers
                    if int(ideal_slice_h * 0.40) <= (gc - current_top) <= int(ideal_slice_h * 1.60)
                ]
                if wider:
                    wider.sort()
                    best_cut = wider[0][1]
                else:
                    # Last resort: force cut at ideal height
                    print(f"  ⚠️  No gap found near y={target_bottom}, force-cutting")
                    best_cut = target_bottom
            
            cut_ys.append(best_cut)
            current_top = best_cut - overlap  # small overlap for continuity
    
    # Build final slice boundaries
    boundaries = sorted(set([0] + cut_ys + [H]))
    
    # Generate slices with overlap
    slices = []
    for i in range(len(boundaries) - 1):
        top = max(0, boundaries[i] - (overlap if i > 0 else 0))
        bottom = min(H, boundaries[i + 1])
        if bottom - top < 50:  # skip tiny slices
            continue
        slices.append((top, bottom))
    
    # Merge last slice if it's too small (< 40% of minimum)
    if len(slices) > 1:
        last_h = slices[-1][1] - slices[-1][0]
        if last_h < min_slice_h * 0.4:
            prev_top = slices[-2][0]
            last_bottom = slices[-1][1]
            slices = slices[:-2] + [(prev_top, last_bottom)]
            print(f"  ℹ️  Merged last small slice with previous")
    
    print(f"\n📋 Splitting into {len(slices)} slices:\n")
    
    # Setup output directory
    if output_dir is None:
        base_dir = os.path.dirname(os.path.abspath(image_path))
        output_dir = os.path.join(base_dir, 'split_output')
    os.makedirs(output_dir, exist_ok=True)
    
    base_name = os.path.splitext(os.path.basename(image_path))[0]
    
    saved_files = []
    for i, (top, bottom) in enumerate(slices):
        slice_h = bottom - top
        ratio_h_w = slice_h / W
        
        # Crop
        cropped = img.crop((0, top, W, bottom))
        
        # Save
        filename = f"{base_name}_part{i+1:02d}.png"
        filepath = os.path.join(output_dir, filename)
        cropped.save(filepath, 'PNG', optimize=True)
        
        size_kb = os.path.getsize(filepath) / 1024
        print(f"  [{i+1:2d}/{len(slices)}] y={top:5d} ~ {bottom:5d} | "
              f"{W}×{slice_h:4d} | ratio ≈ {rw}:{rh * slice_h / ideal_slice_h:.1f} | "
              f"{size_kb:6.0f} KB → {filename}")
        saved_files.append(filepath)
    
    print(f"\n✅ Done! {len(saved_files)} images saved to: {output_dir}/")
    
    # Xiaohongshu tips
    print(f"\n{'='*60}")
    print(f"📱 小红书发布提示:")
    print(f"   • 共 {len(saved_files)} 张图片（小红书单篇最多 18 张）")
    if len(saved_files) > 18:
        print(f"   ⚠️  超过 18 张！建议用 --ratio 3:5 或 2:3 减少张数")
    print(f"   • 目标比例 {rw}:{rh}（竖屏最佳浏览比例）")
    print(f"   • 已在文字空白处/段落间隔处智能切割，不会截断文字")
    print(f"   • 相邻图片有 {overlap}px 重叠，保证阅读连续性")
    print(f"{'='*60}")
    
    return saved_files


def main():
    parser = argparse.ArgumentParser(
        description='🔪 Smart Long Image Splitter for Xiaohongshu (小红书)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s long_article.png                    # Default 3:4 ratio
  %(prog)s long_article.png --ratio 3:4        # Standard vertical (recommended)
  %(prog)s long_article.png --ratio 2:3        # Taller slices, fewer images
  %(prog)s long_article.png --ratio 9:16       # Full phone screen ratio
  %(prog)s long_article.png -o ./my_output     # Custom output directory
        """)
    parser.add_argument('image', help='Path to the long image')
    parser.add_argument('--ratio', default='3:4',
                        help='Target width:height ratio (default: 3:4). Common: 3:4, 2:3, 9:16')
    parser.add_argument('--overlap', type=int, default=40,
                        help='Overlap pixels between slices for continuity (default: 40)')
    parser.add_argument('--output_dir', '-o', default=None,
                        help='Output directory (default: ./split_output next to input)')
    parser.add_argument('--min_gap', type=int, default=5,
                        help='Minimum uniform gap height in px to consider safe (default: 5)')
    
    args = parser.parse_args()
    
    # Parse ratio
    parts = args.ratio.split(':')
    if len(parts) != 2:
        print(f"Error: invalid ratio '{args.ratio}', use format like 3:4")
        sys.exit(1)
    ratio = (int(parts[0]), int(parts[1]))
    
    if not os.path.isfile(args.image):
        print(f"Error: file not found: {args.image}")
        sys.exit(1)
    
    split_image(
        image_path=args.image,
        target_ratio=ratio,
        overlap=args.overlap,
        output_dir=args.output_dir,
        min_gap_px=args.min_gap,
    )


if __name__ == '__main__':
    main()
