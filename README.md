# DiskCleanUp

DiskCleanUp is a local-first CLI for finding redundant video files in specific directories. It is designed for cautious cleanup: scan files, cache video fingerprints, generate a cleanup plan, and move redundant files into a quarantine directory instead of deleting them.

## What it detects

- Exact duplicates: same file hash.
- Near duplicates: same content after transcode, resize, or container changes.
- Containment: a shorter video is mostly covered by a longer video.
- Partial overlaps: reported for review, not moved automatically.

The project uses `ffprobe` for metadata and `ffmpeg` for frame extraction. The Python code computes perceptual frame hashes from tiny grayscale frames, so there are no Python runtime dependencies.

## Requirements

- Python 3.11+
- `ffmpeg` and `ffprobe` on `PATH`

## Quick start

Install the CLI first:

```powershell
python -m pip install -e .
```

Then scan, report, plan, and move:

```powershell
diskcleanup scan D:\Videos
diskcleanup report
diskcleanup plan --output cleanup-plan.json
diskcleanup move --plan cleanup-plan.json --dry-run
diskcleanup move --plan cleanup-plan.json --apply --quarantine D:\VideoQuarantine
```

## Safety model

`move` defaults to dry-run. To change files on disk, pass `--apply`. Moved files are placed under the quarantine directory with a manifest at `.diskcleanup/move-manifest.jsonl`.

## Matching notes

The default scanner samples one frame every two seconds. This keeps scans fast enough for large folders while still catching common duplicate and containment cases. For short clips or aggressive editing, use a smaller interval:

```powershell
python -m diskcleanup scan D:\Videos --interval 1
python -m diskcleanup plan --min-overlap 0.85 --hash-distance 12
```

Changing `--interval` causes cached fingerprints to be rebuilt for unchanged files.
