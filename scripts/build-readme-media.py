#!/usr/bin/env python3
"""Copy supplied stills and export short, original-speed README video previews."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'docs/media/showcase'
IMAGES = {
    '台架渲染2+快拆结构.jpg': 'arm-rig-quick-release.jpg',
    '胸腔台架渲染.jpg': 'torso-rig.jpg',
    '腿部台架渲染.jpg': 'leg-load-rig.jpg',
    '手臂测试数据大屏.jpg': 'arm-dashboard.jpg',
    '下肢测试数据大屏.jpg': 'lower-body-dashboard.jpg',
    '上位机1.jpg': 'plc-control.jpg',
    '上位机2.jpg': 'aging-workspace.jpg',
    '上位机3.jpg': 'motor-monitor.jpg',
}
VIDEOS = {
    '2.5kg单臂负载.mp4': ('arm-load-2_5kg.gif', 2, 6),
    '3.5kg单臂负载.mp4': ('arm-load-3_5kg.gif', 3, 6),
    '5.5kg单臂负载.mp4': ('arm-load-5_5kg.gif', 3, 6),
    '50kg负载踏步.mp4': ('leg-load-50kg-stepping.gif', 2, 6),
    '50kg负载顿起.mp4': ('leg-load-50kg-rise.gif', 1, 6),
    '上肢3ms跑步测试视频.mp4': ('upper-body-dynamic-front.gif', 1, 6),
    '上肢3ms跑步视频2.mp4': ('upper-body-dynamic-side.gif', 1, 6),
}


def sha(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', type=Path)
    parser.add_argument('--ffmpeg', default=shutil.which('ffmpeg'))
    parser.add_argument('--check', action='store_true', help='Verify committed media against their manifest')
    args = parser.parse_args()
    if args.check:
        manifest = json.loads((OUTPUT / 'manifest.json').read_text())
        for row in manifest['assets']:
            path = OUTPUT / row['output']
            if path.stat().st_size != row['output_bytes'] or sha(path) != row['output_sha256']:
                raise SystemExit('Media mismatch: ' + row['output'])
        print(f"PASS {len(manifest['assets'])} README media files")
        return
    if args.source_dir is None or not args.ffmpeg:
        parser.error('Provide --source-dir and an available ffmpeg executable')
    missing = [name for name in (*IMAGES, *VIDEOS) if not (args.source_dir / name).is_file()]
    if missing:
        parser.error('Missing input files: ' + ', '.join(missing))
    OUTPUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for source_name, target_name in IMAGES.items():
        source = args.source_dir / source_name
        target = OUTPUT / target_name
        shutil.copyfile(source, target)
        rows.append({'source': source_name, 'source_bytes': source.stat().st_size, 'source_sha256': sha(source),
                     'output': target_name, 'output_bytes': target.stat().st_size, 'output_sha256': sha(target),
                     'transform': 'Unmodified supplied JPEG'})
    for source_name, (target_name, start, duration) in VIDEOS.items():
        source = args.source_dir / source_name
        target = OUTPUT / target_name
        filters = 'fps=8,scale=300:-2:flags=lanczos,split[a][b];[a]palettegen=max_colors=64:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle'
        subprocess.run([args.ffmpeg, '-hide_banner', '-loglevel', 'error', '-threads', '2',
                        '-ss', str(start), '-i', str(source), '-t', str(duration),
                        '-filter_complex', filters, '-an', '-loop', '0', '-y', str(target)], check=True)
        rows.append({'source': source_name, 'source_bytes': source.stat().st_size, 'source_sha256': sha(source),
                     'output': target_name, 'output_bytes': target.stat().st_size, 'output_sha256': sha(target),
                     'transform': 'Original-speed excerpt; aspect ratio and display rotation preserved; no audio',
                     'clip_start_seconds': start, 'clip_duration_seconds': duration, 'fps': 8, 'width': 300})
        print(target_name, target.stat().st_size, flush=True)
    manifest = {'format': 'rp1-readme-media/1', 'assets': rows}
    (OUTPUT / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    print('PASS copied 8 stills and exported 7 video previews')


if __name__ == '__main__':
    main()
