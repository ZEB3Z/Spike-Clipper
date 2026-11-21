import json
import math
import re
import shutil
import subprocess
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
import numpy as np


BASE_DIR = Path(__file__).parent.resolve()

HYPE_PATTERNS = re.compile(
    r"\b(KEKW|LUL|LULW|OMEGALUL|POG|POGGERS|POGU|CLIP|CLIP IT|NO WAY|WTF|W|WWWW+|INSANE|HUH|ICANT|WHAT|BRO|GG|SHEESH)\b|[!?]{3,}",
    re.IGNORECASE,
)

CAM_PRESETS: Dict[str, Dict[str, float]] = {
    "bottom-left": {"x": 0.0, "y": 0.70, "w": 0.26, "h": 0.30},
    "top-left": {"x": 0.0, "y": 0.0, "w": 0.26, "h": 0.30},
    "bottom-right": {"x": 0.74, "y": 0.70, "w": 0.26, "h": 0.30},
    "top-right": {"x": 0.74, "y": 0.0, "w": 0.26, "h": 0.30},
}


@dataclass
class ClipCandidate:
    id: str
    rank: int
    start_sec: float
    peak_sec: float
    end_sec: float
    duration_sec: float
    peak_msgs_per_sec: float
    z_score: float
    sample_messages: List[str]
    exports: List[dict] = field(default_factory=list)


def normalize_cam_rect(
    cam_rect: Optional[dict] = None,
    cam_corner: str = "bottom-left",
) -> Dict[str, float]:
    if cam_rect and all(k in cam_rect for k in ("x", "y", "w", "h")):
        w = max(0.05, min(0.996, float(cam_rect["w"])))
        h = max(0.05, min(0.996, float(cam_rect["h"])))
        x = max(0.0, min(0.998 - w, float(cam_rect["x"])))
        y = max(0.0, min(0.998 - h, float(cam_rect["y"])))
        return {
            "x": round(x, 4),
            "y": round(y, 4),
            "w": round(w, 4),
            "h": round(h, 4),
        }
    preset = CAM_PRESETS.get(cam_corner, CAM_PRESETS["bottom-left"])
    return {
        "x": round(float(preset["x"]), 4),
        "y": round(float(preset["y"]), 4),
        "w": round(float(preset["w"]), 4),
        "h": round(float(preset["h"]), 4),
    }


def extract_vod_id(vod_input: str) -> str:
    vod_input = str(vod_input).strip()
    if vod_input.isdigit():
        return vod_input
    match = re.search(r"twitch\.tv/videos/(\d+)", vod_input)
    if match:
        return match.group(1)
    raise ValueError("Invalid Twitch VOD URL or ID. Expected format: https://www.twitch.tv/videos/1234567890")


def format_timestamp(seconds: float) -> str:
    total = max(0, int(round(float(seconds))))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def check_binary(name: str) -> Optional[str]:
    for candidate_dir in (BASE_DIR, Path.cwd()):
        for ext in (".exe", ""):
            candidate = candidate_dir / f"{name}{ext}"
            if candidate.is_file():
                return str(candidate)
    return shutil.which(name)


def download_chat_json(vod_id: str, work_dir: Path) -> Path:
    chat_path = work_dir / f"chat_{vod_id}.json"
    if chat_path.exists() and chat_path.stat().st_size > 100:
        return chat_path

    cli = check_binary("TwitchDownloaderCLI")
    if not cli:
        raise RuntimeError(
            "TwitchDownloaderCLI.exe not found in project folder or system PATH."
        )

    cmd = [cli, "chatdownload", "--id", str(vod_id), "-o", str(chat_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not chat_path.exists():
        raise RuntimeError(f"Chat download failed: {proc.stderr or proc.stdout}")
    return chat_path


def download_vod_slice(vod_id: str, start_sec: float, end_sec: float, out_path: Path) -> None:
    if out_path.exists() and out_path.stat().st_size > 1024:
        return

    cli = check_binary("TwitchDownloaderCLI")
    ytdlp = check_binary("yt-dlp")
    errors: List[str] = []

    b_sec = max(0, int(math.floor(float(start_sec))))
    e_sec = max(b_sec + 1, int(math.ceil(float(end_sec))))

    if cli:
        cmd = [
            cli,
            "videodownload",
            "--id",
            str(vod_id),
            "-b",
            str(b_sec),
            "-e",
            str(e_sec),
            "-o",
            str(out_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 1024:
            return
        errors.append(f"TwitchDownloaderCLI: {proc.stderr or proc.stdout}")

    if ytdlp:
        section = f"*{format_timestamp(b_sec)}-{format_timestamp(e_sec)}"
        cmd = [
            ytdlp,
            "--download-sections",
            section,
            "-f",
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "-o",
            str(out_path),
            f"https://www.twitch.tv/videos/{vod_id}",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 1024:
            return
        errors.append(f"yt-dlp: {proc.stderr or proc.stdout}")

    if not cli and not ytdlp:
        raise RuntimeError("Neither TwitchDownloaderCLI.exe nor yt-dlp was found.")
    raise RuntimeError("Segment download failed: " + " | ".join(errors))


def analyze_chat_spikes(
    chat_path: Path,
    vod_id: str,
    bin_size: int = 5,
    z_threshold: float = 2.2,
    chat_delay_sec: float = -7.0,
    pre_pad: int = 18,
    post_pad: int = 10,
    merge_gap: int = 15,
    max_clips: int = 12,
    existing_manifest: Optional[dict] = None,
) -> dict:
    with open(chat_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    comments = raw.get("comments", [])
    if not comments:
        raise ValueError("No chat messages found in this VOD.")

    offsets: List[float] = []
    weights: List[float] = []
    parsed_msgs: List[tuple] = []

    for c in comments:
        offset = float(c.get("content_offset_seconds", 0.0))
        body = str((c.get("message") or {}).get("body", ""))
        user = str((c.get("commenter") or {}).get("display_name", "anon"))
        if offset < 0.0:
            continue

        hype_hits = len(HYPE_PATTERNS.findall(body))
        is_caps = len(body) >= 5 and body.isupper()
        weight = 1.0 + (0.45 * min(hype_hits, 3)) + (0.25 if is_caps else 0.0)

        offsets.append(offset)
        weights.append(float(weight))
        parsed_msgs.append((offset, user, body))

    if not offsets:
        raise ValueError("No valid timestamped chat messages found in this VOD.")

    max_sec = float(max(offsets))
    num_bins = int(math.ceil((max_sec + 1.0) / float(bin_size)))
    counts = np.zeros(num_bins, dtype=float)
    weighted_scores = np.zeros(num_bins, dtype=float)

    for off, w in zip(offsets, weights):
        idx = min(int(off // bin_size), num_bins - 1)
        counts[idx] += 1.0
        weighted_scores[idx] += w

    window_bins = max(6, int(90 // bin_size))
    z_scores = np.zeros(num_bins, dtype=float)

    for i in range(num_bins):
        lo = max(0, i - window_bins)
        hi = min(num_bins, i + window_bins + 1)
        local = weighted_scores[lo:hi]
        mean = float(np.mean(local))
        std = float(np.std(local))
        z_scores[i] = (float(weighted_scores[i]) - mean) / max(std, 1.5)

    spike_bins = np.where((z_scores >= float(z_threshold)) & (counts >= 3))[0]

    merged_windows: List[List[int]] = []
    for raw_b in spike_bins:
        b = int(raw_b)
        b_start = int(b * bin_size)
        b_end = int((b + 1) * bin_size)
        if not merged_windows:
            merged_windows.append([b_start, b_end, b])
        else:
            prev_start, prev_end, prev_peak_bin = merged_windows[-1]
            if b_start - prev_end <= int(merge_gap):
                best_bin = b if float(weighted_scores[b]) > float(weighted_scores[prev_peak_bin]) else int(prev_peak_bin)
                merged_windows[-1] = [int(prev_start), int(b_end), int(best_bin)]
            else:
                merged_windows.append([b_start, b_end, b])

    prior_exports_by_id: Dict[str, List[dict]] = {}
    saved_cam_rect = normalize_cam_rect(
        existing_manifest.get("cam_rect") if existing_manifest else None
    )

    if existing_manifest:
        for old_clip in existing_manifest.get("clips", []):
            cid = str(old_clip.get("id", ""))
            valid_exports = [
                exp for exp in old_clip.get("exports", [])
                if (BASE_DIR / str(exp.get("video_url", "")).split("?")[0].lstrip("/")).exists()
            ]
            if cid and valid_exports:
                prior_exports_by_id[cid] = valid_exports

    candidates: List[ClipCandidate] = []
    for idx, (w_start, w_end, peak_bin) in enumerate(merged_windows):
        peak_sec = float(round(float(peak_bin * bin_size) + (float(bin_size) / 2.0), 1))

        raw_start = float(w_start) - float(pre_pad) + float(chat_delay_sec)
        raw_end = float(w_end) + float(post_pad) + float(chat_delay_sec)

        clip_start = float(round(min(max(0.0, raw_start), max(0.0, max_sec - 1.0)), 1))
        clip_end = float(round(min(max_sec, max(clip_start + 1.0, raw_end)), 1))

        peak_mps = float(round(float(counts[peak_bin]) / float(bin_size), 2))
        peak_z = float(round(float(z_scores[peak_bin]), 2))

        samples = [
            f"{u}: {m}"
            for (off, u, m) in parsed_msgs
            if abs(float(off) - peak_sec) <= 8.0 and len(m.strip()) > 0
        ][:5]

        clip_id = f"clip_{idx + 1:02d}"
        candidates.append(
            ClipCandidate(
                id=clip_id,
                rank=0,
                start_sec=clip_start,
                peak_sec=peak_sec,
                end_sec=clip_end,
                duration_sec=float(round(clip_end - clip_start, 1)),
                peak_msgs_per_sec=peak_mps,
                z_score=peak_z,
                sample_messages=samples,
                exports=prior_exports_by_id.get(clip_id, []),
            )
        )

    candidates.sort(key=lambda c: c.z_score, reverse=True)
    candidates = candidates[: int(max_clips)]
    for rank_i, c in enumerate(candidates, start=1):
        c.rank = int(rank_i)

    step = max(1, int(num_bins // 400))
    timeline = [
        {
            "sec": int(i * bin_size),
            "label": format_timestamp(i * bin_size),
            "mps": float(round(float(np.mean(counts[i : i + step])) / float(bin_size), 2)),
            "z": float(round(float(np.max(z_scores[i : i + step])), 2)),
        }
        for i in range(0, num_bins, step)
    ]

    preview_file = chat_path.parent / f"preview_{vod_id}.jpg"
    preview_url = f"/outputs/{vod_id}/{preview_file.name}" if preview_file.exists() else None

    return {
        "vod_id": str(vod_id),
        "vod_url": f"https://www.twitch.tv/videos/{vod_id}",
        "total_messages": int(len(offsets)),
        "duration_sec": float(round(max_sec, 1)),
        "duration_label": format_timestamp(max_sec),
        "chat_delay_sec": float(chat_delay_sec),
        "pre_pad": int(pre_pad),
        "post_pad": int(post_pad),
        "z_threshold": float(z_threshold),
        "cam_rect": saved_cam_rect,
        "preview_url": preview_url,
        "timeline": timeline,
        "clips": [asdict(c) for c in candidates],
    }


def grab_preview_frame(vod_id: str, timestamp_sec: float, work_dir: Path) -> dict:
    ffmpeg = check_binary("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg not found in system PATH.")

    ts = max(0.0, float(timestamp_sec))
    slice_start = max(0.0, ts - 1.0)
    slice_end = slice_start + 3.0

    tmp_slice = work_dir / f"_preview_slice_{int(ts)}.mp4"
    preview_jpg = work_dir / f"preview_{vod_id}.jpg"

    try:
        download_vod_slice(vod_id, slice_start, slice_end, tmp_slice)
        seek_offset = max(0.0, ts - slice_start)
        cmd = [
            ffmpeg,
            "-y",
            "-ss",
            f"{seek_offset:.2f}",
            "-i",
            tmp_slice.name,
            "-frames:v",
            "1",
            "-q:v",
            "2",
            preview_jpg.name,
        ]
        proc = subprocess.run(cmd, cwd=str(work_dir), capture_output=True, text=True)
        if proc.returncode != 0 or not preview_jpg.exists():
            fallback_cmd = [
                ffmpeg,
                "-y",
                "-i",
                tmp_slice.name,
                "-frames:v",
                "1",
                "-q:v",
                "2",
                preview_jpg.name,
            ]
            proc2 = subprocess.run(fallback_cmd, cwd=str(work_dir), capture_output=True, text=True)
            if proc2.returncode != 0 or not preview_jpg.exists():
                raise RuntimeError(f"FFmpeg frame extraction failed: {proc2.stderr}")
    finally:
        if tmp_slice.exists():
            try:
                tmp_slice.unlink()
            except OSError:
                pass

    return {
        "preview_url": f"/outputs/{vod_id}/{preview_jpg.name}?t={int(datetime.now(timezone.utc).timestamp())}",
        "timestamp_sec": float(round(ts, 1)),
        "timestamp_label": format_timestamp(ts),
    }


def _format_ass_time(seconds: float) -> str:
    total_cs = max(0, int(round(float(seconds) * 100)))
    cs = total_cs % 100
    total_s = total_cs // 100
    s = total_s % 60
    m = (total_s // 60) % 60
    h = total_s // 3600
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _escape_ass_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def generate_ass_captions(video_path: Path, ass_path: Path, is_vertical: bool = True) -> bool:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return False

    model = WhisperModel("base", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(
        str(video_path),
        beam_size=1,
        vad_filter=True,
        word_timestamps=True,
    )

    words = []
    for seg in segments:
        if getattr(seg, "words", None):
            for w in seg.words:
                token = str(w.word).strip()
                if token:
                    words.append({
                        "text": token.upper(),
                        "start": float(w.start),
                        "end": max(float(w.end), float(w.start) + 0.08),
                    })
        else:
            text = str(seg.text).strip()
            if text:
                words.append({
                    "text": text.upper(),
                    "start": float(seg.start),
                    "end": max(float(seg.end), float(seg.start) + 0.2),
                })

    if not words:
        return False

    chunks: List[List[dict]] = []
    current_chunk: List[dict] = []

    for w in words:
        if not current_chunk:
            current_chunk.append(w)
            continue

        prev = current_chunk[-1]
        pause_gap = w["start"] - prev["end"]
        ends_clause = prev["text"].endswith((".", "!", "?", ","))

        if len(current_chunk) >= 4 or (len(current_chunk) >= 2 and (ends_clause or pause_gap > 0.35)):
            chunks.append(current_chunk)
            current_chunk = [w]
        else:
            current_chunk.append(w)

    if current_chunk:
        chunks.append(current_chunk)

    play_x = 1080 if is_vertical else 1920
    play_y = 1920 if is_vertical else 1080
    font_size = 76 if is_vertical else 56
    margin_v = 340 if is_vertical else 85

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {play_x}
PlayResY: {play_y}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,{font_size},&H00FFFFFF,&H0000FFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,4,2,2,40,40,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    dialogue_lines: List[str] = []
    for chunk in chunks:
        for active_idx, active_word in enumerate(chunk):
            ev_start = active_word["start"]
            if active_idx + 1 < len(chunk):
                ev_end = max(active_word["end"], chunk[active_idx + 1]["start"])
            else:
                ev_end = active_word["end"] + 0.12

            styled_tokens: List[str] = []
            for idx_in_chunk, token_obj in enumerate(chunk):
                clean_tok = _escape_ass_text(token_obj["text"])
                if idx_in_chunk == active_idx:
                    styled_tokens.append(
                        f"{{\\c&H0000FFFF&\\fscx108\\fscy108}}{clean_tok}{{\\c&H00FFFFFF&\\fscx100\\fscy100}}"
                    )
                else:
                    styled_tokens.append(clean_tok)

            line_text = " ".join(styled_tokens)
            dialogue_lines.append(
                f"Dialogue: 0,{_format_ass_time(ev_start)},{_format_ass_time(ev_end)},Default,,0,0,0,,{line_text}"
            )

    ass_path.write_text(header + "\n".join(dialogue_lines) + "\n", encoding="utf-8")
    return True


def render_clip(
    vod_id: str,
    clip_id: str,
    start_sec: float,
    end_sec: float,
    work_dir: Path,
    layout: str = "16:9",
    cam_corner: str = "bottom-left",
    cam_rect: Optional[dict] = None,
    burn_captions: bool = False,
    normalize_audio: bool = True,
) -> dict:
    ffmpeg = check_binary("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg not found in system PATH.")

    start_f = max(0.0, float(start_sec))
    end_f = max(start_f + 1.0, float(end_sec))
    b_int = int(math.floor(start_f))
    e_int = int(math.ceil(end_f))

    rect = normalize_cam_rect(cam_rect=cam_rect, cam_corner=cam_corner)
    rect_label = (
        f"x{int(round(rect['x'] * 100))}-y{int(round(rect['y'] * 100))}"
        f"-w{int(round(rect['w'] * 100))}-h{int(round(rect['h'] * 100))}"
    )

    layout_slug = layout.replace(":", "x")
    if layout == "9:16-split":
        variant_tag = f"{layout_slug}_{rect_label}_{b_int}-{e_int}"
    else:
        variant_tag = f"{layout_slug}_{b_int}-{e_int}"

    raw_mp4 = work_dir / f"{clip_id}_{b_int}-{e_int}_raw.mp4"
    final_mp4 = work_dir / f"{clip_id}_{variant_tag}.mp4"
    contact_png = work_dir / f"{clip_id}_{variant_tag}_contact.png"
    ass_file = work_dir / f"{clip_id}_{b_int}-{e_int}_{'vert' if layout.startswith('9:16') else 'wide'}.ass"

    # 1. Download ONLY the clipped time range
    download_vod_slice(vod_id, start_f, end_f, raw_mp4)

    # 2. Optional word-by-word .ass karaoke captions via faster-whisper
    has_ass = False
    is_vertical = layout.startswith("9:16")
    if burn_captions:
        has_ass = generate_ass_captions(raw_mp4, ass_file, is_vertical=is_vertical)

    # Relative filename only for Windows FFmpeg filtergraph safety
    ass_clause = f",ass={ass_file.name}" if has_ass else ""
    audio_args = (
        ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "160k"]
        if normalize_audio
        else ["-c:a", "aac", "-b:a", "160k"]
    )

    # 3. Build deterministic FFmpeg filter graph
    if layout == "9:16-crop":
        vf = f"crop=ih*9/16:ih,scale=1080:1920,setsar=1{ass_clause}"
        ff_cmd = [
            ffmpeg, "-y", "-i", raw_mp4.name,
            "-vf", vf,
            "-c:v", "libx264", "-crf", "20", "-preset", "fast", "-pix_fmt", "yuv420p",
            *audio_args,
            final_mp4.name,
        ]
    elif layout == "9:16-split":
        cam_crop = (
            f"crop=iw*{rect['w']:.4f}:ih*{rect['h']:.4f}:iw*{rect['x']:.4f}:ih*{rect['y']:.4f}"
        )
        fc = (
            f"[0:v]{cam_crop},scale=1080:620,setsar=1[cam];"
            f"[0:v]crop=ih*0.83:ih,scale=1080:1300,setsar=1[game];"
            f"[cam][game]vstack=inputs=2{ass_clause}[out]"
        )
        ff_cmd = [
            ffmpeg, "-y", "-i", raw_mp4.name,
            "-filter_complex", fc,
            "-map", "[out]", "-map", "0:a?",
            "-c:v", "libx264", "-crf", "20", "-preset", "fast", "-pix_fmt", "yuv420p",
            *audio_args,
            final_mp4.name,
        ]
    else:
        if has_ass:
            ff_cmd = [
                ffmpeg, "-y", "-i", raw_mp4.name,
                "-vf", f"ass={ass_file.name}",
                "-c:v", "libx264", "-crf", "20", "-preset", "fast", "-pix_fmt", "yuv420p",
                *audio_args,
                final_mp4.name,
            ]
        elif normalize_audio:
            ff_cmd = [
                ffmpeg, "-y", "-i", raw_mp4.name,
                "-c:v", "copy",
                "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
                "-c:a", "aac", "-b:a", "160k",
                final_mp4.name,
            ]
        else:
            ff_cmd = [
                ffmpeg, "-y", "-i", raw_mp4.name,
                "-c", "copy",
                final_mp4.name,
            ]

    proc = subprocess.run(ff_cmd, cwd=str(work_dir), capture_output=True, text=True)
    if proc.returncode != 0 or not final_mp4.exists():
        raise RuntimeError(f"FFmpeg render failed: {proc.stderr}")

    # 4. Generate 4x2 QC contact sheet
    duration = max(1.0, end_f - start_f)
    sample_fps = max(0.1, 8.0 / duration)
    qc_cmd = [
        ffmpeg, "-y", "-i", final_mp4.name,
        "-vf", f"fps={sample_fps:.4f},scale=270:-1,tile=4x2",
        "-frames:v", "1",
        contact_png.name,
    ]
    subprocess.run(qc_cmd, cwd=str(work_dir), capture_output=True, text=True)

    return {
        "clip_id": str(clip_id),
        "layout": str(layout),
        "cam_corner": str(cam_corner),
        "cam_rect": rect,
        "cam_label": (
            f"{int(round(rect['w'] * 100))}x{int(round(rect['h'] * 100))}%"
            f" @ ({int(round(rect['x'] * 100))},{int(round(rect['y'] * 100))}%)"
        ),
        "captioned": bool(has_ass),
        "normalized": bool(normalize_audio),
        "start_sec": float(round(start_f, 1)),
        "end_sec": float(round(end_f, 1)),
        "video_url": f"/outputs/{vod_id}/{final_mp4.name}",
        "contact_url": f"/outputs/{vod_id}/{contact_png.name}" if contact_png.exists() else None,
        "filename": final_mp4.name,
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }