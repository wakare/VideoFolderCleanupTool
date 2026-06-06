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
diskcleanup scan D:\Videos --fingerprint-mode seek --interval 20 --workers 4
diskcleanup report
diskcleanup plan --output cleanup-plan.json
diskcleanup move --plan cleanup-plan.json --dry-run
diskcleanup move --plan cleanup-plan.json --apply --quarantine D:\VideoQuarantine
```

## Safety model

`move` defaults to dry-run. To change files on disk, pass `--apply`. Moved files are placed under the quarantine directory with a manifest at `.diskcleanup/move-manifest.jsonl`.

## Matching notes

The default scanner uses continuous FFmpeg sampling and samples one frame every two seconds. This is accurate but slow for large folders because FFmpeg still has to decode through each video.

For 1000+ video batches, use seek-based coarse sampling first:

```powershell
diskcleanup scan D:\Videos --fingerprint-mode seek --interval 20 --workers 4 --hash-mode quick
diskcleanup report --candidate-mode indexed
```

If PyAV is installed, the optional PyAV seek backend can avoid starting one FFmpeg process per sampled frame:

```powershell
python -m pip install -e ".[pyav]"
diskcleanup scan D:\Videos --fingerprint-mode pyav-seek --interval 20 --workers 4 --hash-mode quick
```

Then rescan only suspicious groups with a smaller interval if needed:

```powershell
diskcleanup scan D:\Videos --fingerprint-mode seek --interval 10 --force
diskcleanup plan --min-overlap 0.85 --hash-distance 12 --candidate-mode indexed
```

Changing `--interval` or `--fingerprint-mode` causes cached fingerprints to be rebuilt for unchanged files.

You can name and inspect fingerprint cache profiles:

```powershell
diskcleanup scan D:\Videos --fingerprint-mode seek --interval 20 --profile-name coarse20
diskcleanup scan D:\Videos --fingerprint-mode seek --interval 10 --profile-name fine10
diskcleanup profiles
diskcleanup report --fingerprint-profile coarse20
```

## Performance strategy

The tool prioritizes cheaper checks before expensive visual comparison:

- `quick_hash`: hashes file size plus head/middle/tail chunks. This is the default and is much faster than full-file SHA256.
- `sha256`: available with `--hash-mode sha256` when exact cryptographic confirmation is required.
- Indexed candidates: `report` and `plan` default to `--candidate-mode indexed`, which uses shared frame-hash anchors and offset votes to avoid exhaustive pairwise comparison.
- Exhaustive fallback: use `--candidate-mode exhaustive` when recall matters more than runtime.
