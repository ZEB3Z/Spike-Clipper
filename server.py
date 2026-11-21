import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from clipper_engine import (
    extract_vod_id,
    download_chat_json,
    analyze_chat_spikes,
    grab_preview_frame,
    normalize_cam_rect,
    render_clip,
)

BASE_DIR = Path(__file__).parent.resolve()
OUTPUTS_DIR = BASE_DIR / "outputs"
STATIC_DIR = BASE_DIR / "static"
OUTPUTS_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Twitch Chat Spike Clipper")
app.mount("/outputs", StaticFiles(directory=str(OUTPUTS_DIR)), name="outputs")


class AnalyzeRequest(BaseModel):
    vod_url: str
    z_threshold: float = 2.2
    chat_delay_sec: float = -7.0
    pre_pad: int = 18
    post_pad: int = 10
    max_clips: int = 12


class PreviewFrameRequest(BaseModel):
    vod_id: Optional[str] = None
    vod_url: Optional[str] = None
    timestamp_sec: Optional[float] = None


class ExportRequest(BaseModel):
    vod_id: str
    clip_id: str
    start_sec: float
    end_sec: float
    layout: str = "16:9"
    cam_corner: str = "bottom-left"
    cam_rect: Optional[Dict[str, float]] = None
    burn_captions: bool = False
    normalize_audio: bool = True


def _validate_vod_id(vod_id: str) -> str:
    clean = str(vod_id).strip()
    if not clean.isdigit():
        raise ValueError("VOD ID must be numeric.")
    return clean


@app.get("/")
def serve_ui():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/history")
def api_history():
    items = []
    for manifest_file in OUTPUTS_DIR.glob("*/manifest.json"):
        try:
            vod_id = manifest_file.parent.name
            if not vod_id.isdigit():
                continue
            data = json.loads(manifest_file.read_text(encoding="utf-8"))
            mtime = manifest_file.stat().st_mtime
            items.append({
                "vod_id": str(data.get("vod_id", vod_id)),
                "duration_label": str(data.get("duration_label", "00:00:00")),
                "total_messages": int(data.get("total_messages", 0)),
                "clip_count": int(len(data.get("clips", []))),
                "modified_time": float(mtime),
                "modified_label": datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            })
        except Exception:
            continue

    items.sort(key=lambda x: x["modified_time"], reverse=True)
    return {"history": items}


@app.get("/api/manifest/{vod_id}")
def api_manifest(vod_id: str):
    try:
        clean_id = _validate_vod_id(vod_id)
        manifest_path = OUTPUTS_DIR / clean_id / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"No cached manifest found for VOD {clean_id}.")

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        preview_file = OUTPUTS_DIR / clean_id / f"preview_{clean_id}.jpg"
        manifest["preview_url"] = (
            f"/outputs/{clean_id}/{preview_file.name}" if preview_file.exists() else None
        )
        manifest["cam_rect"] = normalize_cam_rect(manifest.get("cam_rect"))

        for clip in manifest.get("clips", []):
            valid_exports = []
            for exp in clip.get("exports", []):
                rel_vid = str(exp.get("video_url", "")).split("?")[0].lstrip("/")
                if rel_vid and (BASE_DIR / rel_vid).exists():
                    valid_exports.append(exp)
            clip["exports"] = valid_exports

        return manifest
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/analyze")
def api_analyze(req: AnalyzeRequest):
    try:
        vod_id = extract_vod_id(req.vod_url)
        vod_dir = OUTPUTS_DIR / vod_id
        vod_dir.mkdir(parents=True, exist_ok=True)

        manifest_path = vod_dir / "manifest.json"
        existing_manifest = None
        if manifest_path.exists():
            try:
                existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                existing_manifest = None

        chat_path = download_chat_json(vod_id, vod_dir)
        manifest = analyze_chat_spikes(
            chat_path=chat_path,
            vod_id=vod_id,
            z_threshold=req.z_threshold,
            chat_delay_sec=req.chat_delay_sec,
            pre_pad=req.pre_pad,
            post_pad=req.post_pad,
            max_clips=req.max_clips,
            existing_manifest=existing_manifest,
        )

        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return manifest
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/preview-frame")
def api_preview_frame(req: PreviewFrameRequest):
    try:
        if req.vod_id:
            vod_id = _validate_vod_id(req.vod_id)
        elif req.vod_url:
            vod_id = extract_vod_id(req.vod_url)
        else:
            raise ValueError("Provide either vod_id or vod_url.")

        vod_dir = OUTPUTS_DIR / vod_id
        vod_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = vod_dir / "manifest.json"

        manifest = None
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        target_ts = req.timestamp_sec
        if target_ts is None:
            if manifest and manifest.get("clips"):
                top_clip = manifest["clips"][0]
                target_ts = float(top_clip.get("peak_sec", 60.0))
            else:
                target_ts = 60.0

        result = grab_preview_frame(vod_id=vod_id, timestamp_sec=float(target_ts), work_dir=vod_dir)

        if manifest is not None:
            manifest["preview_url"] = result["preview_url"]
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/export")
def api_export(req: ExportRequest):
    try:
        vod_id = _validate_vod_id(req.vod_id)
        vod_dir = OUTPUTS_DIR / vod_id
        vod_dir.mkdir(parents=True, exist_ok=True)

        result = render_clip(
            vod_id=vod_id,
            clip_id=req.clip_id,
            start_sec=req.start_sec,
            end_sec=req.end_sec,
            work_dir=vod_dir,
            layout=req.layout,
            cam_corner=req.cam_corner,
            cam_rect=req.cam_rect,
            burn_captions=req.burn_captions,
            normalize_audio=req.normalize_audio,
        )

        manifest_path = vod_dir / "manifest.json"
        updated_exports = [result]
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["cam_rect"] = result["cam_rect"]
                for clip in manifest.get("clips", []):
                    if clip.get("id") == req.clip_id:
                        clip["start_sec"] = float(round(req.start_sec, 1))
                        clip["end_sec"] = float(round(req.end_sec, 1))
                        clip["duration_sec"] = float(round(max(1.0, req.end_sec - req.start_sec), 1))
                        existing = [
                            e for e in clip.get("exports", [])
                            if e.get("filename") != result["filename"]
                        ]
                        existing.insert(0, result)
                        clip["exports"] = existing
                        updated_exports = existing
                        break
                manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            except Exception:
                pass

        return {**result, "exports": updated_exports}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)