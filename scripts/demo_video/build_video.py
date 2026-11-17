"""Edit the recorded NEXUS AI browser session into the final demo MP4.

Input: the output directory of ``record_demo.mjs`` (JPEG screencast frames with capture timestamps
and ``markers.json``). Output: an H.264 MP4 with a title card, section labels, captions, short
cross-fades and a closing card. Only time ranges of the real recording are used; waiting time is
removed with visible jump cuts (cross-fades), never replaced by generated content.

    python build_video.py <recording-dir> <output.mp4> [--thumbnail <file.png>]

Requires Pillow and an FFmpeg build with libx264 (``FFMPEG`` env var, ``imageio-ffmpeg`` or PATH).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1920, 1080
BX, BY, BW, BH = 160, 76, 1600, 900   # browser area inside the canvas
FPS = 30
XFADE = 0.4
BG_TOP, BG_BOTTOM = (10, 14, 30), (20, 26, 52)
ACCENT = (129, 140, 248)
TEXT = (241, 245, 249)
MUTED = (148, 163, 184)
REPO_SLUG = "github.com/KrzakLara/nexus-ai-enterprise-rag"


def ffmpeg_exe() -> str:
    if os.environ.get("FFMPEG"):
        return os.environ["FFMPEG"]
    try:
        import imageio_ffmpeg  # type: ignore
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        found = shutil.which("ffmpeg")
        if not found:
            sys.exit("FFmpeg not found: set FFMPEG or install imageio-ffmpeg")
        return found


FF = ffmpeg_exe()


def run(args: list[str]) -> None:
    proc = subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True, text=True)
    if proc.returncode:
        sys.exit(f"ffmpeg failed: {' '.join(args)}\n{proc.stderr}")


# ------------------------------------------------------------------------------------ typography
FONT_DIRS = [Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu"), Path("/Library/Fonts")]
FONT_FILES = {
    "regular": ["segoeui.ttf", "DejaVuSans.ttf", "Arial.ttf"],
    "semibold": ["seguisb.ttf", "DejaVuSans-Bold.ttf", "Arial Bold.ttf"],
    "bold": ["segoeuib.ttf", "DejaVuSans-Bold.ttf", "Arial Bold.ttf"],
    "light": ["segoeuil.ttf", "DejaVuSans.ttf", "Arial.ttf"],
}


def font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    for name in FONT_FILES[kind]:
        for d in FONT_DIRS:
            if (d / name).exists():
                return ImageFont.truetype(str(d / name), size)
    return ImageFont.load_default(size)


def gradient() -> Image.Image:
    img = Image.new("RGB", (W, H))
    px = ImageDraw.Draw(img)
    for y in range(H):
        k = y / (H - 1)
        px.line([(0, y), (W, y)], fill=tuple(int(a + (b - a) * k) for a, b in zip(BG_TOP, BG_BOTTOM)))
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((-300, -420, 900, 520), fill=(99, 102, 241, 60))
    gd.ellipse((1250, 640, 2300, 1500), fill=(14, 165, 233, 42))
    glow = glow.filter(ImageFilter.GaussianBlur(160))
    return Image.alpha_composite(img.convert("RGBA"), glow)


def logo(draw: ImageDraw.ImageDraw, x: int, y: int, s: int) -> None:
    """Four-square mark (same idea as the app's logo symbol)."""
    g = s // 8
    c = (s - g) // 2
    colors = [(129, 140, 248), (56, 189, 248), (167, 139, 250), (99, 102, 241)]
    for i, col in enumerate(colors):
        dx, dy = (i % 2) * (c + g), (i // 2) * (c + g)
        draw.rounded_rectangle((x + dx, y + dy, x + dx + c, y + dy + c), radius=max(3, c // 4), fill=col)


def wordmark(draw: ImageDraw.ImageDraw, x: int, y: int, size: int) -> int:
    f = font("bold", size)
    draw.text((x, y), "NEXUS", font=f, fill=TEXT)
    w = draw.textlength("NEXUS ", font=f)
    draw.text((x + w, y), "AI", font=f, fill=ACCENT)
    return int(w + draw.textlength("AI", font=f))


def frame_overlay(path: Path) -> None:
    """Canvas with a rounded hole for the browser recording (drawn on top of the video)."""
    base = gradient()
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((BX - 4, BY + 10, BX + BW + 4, BY + BH + 18), radius=22, fill=(0, 0, 0, 150))
    base = Image.alpha_composite(base, shadow.filter(ImageFilter.GaussianBlur(18)))
    d = ImageDraw.Draw(base)
    d.rounded_rectangle((BX - 2, BY - 2, BX + BW + 1, BY + BH + 1), radius=16, outline=(71, 85, 125, 255), width=2)
    mask = Image.new("L", (W, H), 255)
    ImageDraw.Draw(mask).rounded_rectangle((BX, BY, BX + BW - 1, BY + BH - 1), radius=14, fill=0)
    # transparent hole for the recording, but keep the outline drawn on top of it
    outline = Image.new("L", (W, H), 0)
    ImageDraw.Draw(outline).rounded_rectangle((BX - 2, BY - 2, BX + BW + 1, BY + BH + 1), radius=16, outline=255, width=2)
    base.putalpha(Image.composite(Image.new("L", (W, H), 255), mask, outline))
    base.save(path)


def caption_overlay(path: Path, index: int, section: str, caption: str) -> None:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # top band: section chip (left), wordmark (right)
    chip_font = font("semibold", 21)
    label = f"{index:02d}  ·  {section.upper()}"
    tw = d.textlength(label, font=chip_font)
    d.rounded_rectangle((BX, 18, BX + tw + 36, 58), radius=20, fill=(99, 102, 241, 46), outline=(129, 140, 248, 170), width=1)
    d.text((BX + 18, 38), label, font=chip_font, fill=(199, 210, 254), anchor="lm")
    logo(d, W - BX - 168, 24, 30)
    wordmark(d, W - BX - 128, 22, 26)
    # bottom band: caption
    cap_font = font("semibold", 31)
    d.text((W // 2, BY + BH + 52), caption, font=cap_font, fill=TEXT, anchor="mm")
    img.save(path)


def title_card(path: Path) -> None:
    img = gradient()
    d = ImageDraw.Draw(img)
    logo(d, W // 2 - 48, 250, 96)
    f = font("bold", 112)
    total = d.textlength("NEXUS AI", font=f)
    x = W // 2 - total / 2
    d.text((x, 400), "NEXUS", font=f, fill=TEXT)
    d.text((x + d.textlength("NEXUS ", font=f), 400), "AI", font=f, fill=ACCENT)
    d.text((W // 2, 590), "Enterprise RAG & Document Intelligence", font=font("light", 46), fill=TEXT, anchor="mm")
    d.text((W // 2, 668), "Hybrid search with citations  ·  OCR & structured extraction  ·  Human review  ·  Tenant isolation",
           font=font("regular", 28), fill=MUTED, anchor="mm")
    d.rounded_rectangle((W // 2 - 330, 760, W // 2 + 330, 812), radius=26, outline=(129, 140, 248, 200), width=2)
    d.text((W // 2, 786), "Real application recording  ·  synthetic data only", font=font("semibold", 24), fill=(199, 210, 254), anchor="mm")
    img.convert("RGB").save(path)


def closing_card(path: Path) -> None:
    img = gradient()
    d = ImageDraw.Draw(img)
    logo(d, W // 2 - 36, 170, 72)
    f = font("bold", 84)
    total = d.textlength("NEXUS AI", font=f)
    x = W // 2 - total / 2
    d.text((x, 270), "NEXUS", font=f, fill=TEXT)
    d.text((x + d.textlength("NEXUS ", font=f), 270), "AI", font=f, fill=ACCENT)
    lines = [
        "Keycloak SSO (OIDC + PKCE) with a backend-for-frontend session",
        "Durable background ingestion: OCR, layout, structured extraction with evidence",
        "Human-in-the-loop review with business-rule validation",
        "Hybrid retrieval (PostgreSQL FTS + pgvector, RRF) with verbatim citations",
        "Tenant & group ACL in the retrieval SQL, plus PostgreSQL row-level security",
        "Prometheus, Grafana and OpenTelemetry tracing",
    ]
    fy = 430
    for line in lines:
        d.ellipse((560, fy + 13, 572, fy + 25), fill=ACCENT)
        d.text((596, fy), line, font=font("regular", 31), fill=TEXT)
        fy += 58
    d.rounded_rectangle((W // 2 - 420, 832, W // 2 + 420, 896), radius=32, fill=(99, 102, 241, 255))
    d.text((W // 2, 864), REPO_SLUG, font=font("semibold", 31), fill=(255, 255, 255), anchor="mm")
    d.text((W // 2, 950), "Python · FastAPI · React · PostgreSQL/pgvector · Docker  ·  all data shown is synthetic",
           font=font("regular", 24), fill=MUTED, anchor="mm")
    img.convert("RGB").save(path)


def thumbnail(path: Path, app_frame: Path, frame_png: Path, duration: float) -> None:
    """A real application frame in the video's canvas, with the title and a play badge."""
    canvas = gradient()
    shot = Image.open(app_frame).convert("RGBA").resize((BW, BH), Image.LANCZOS)
    canvas.alpha_composite(shot, (BX, BY))
    canvas.alpha_composite(Image.open(frame_png).convert("RGBA"))
    veil = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    vd = ImageDraw.Draw(veil)
    for y in range(H):  # dark gradient from the bottom for the title
        a = int(min(1.0, max(0.0, (y - 360) / 260)) * 242)
        vd.line([(0, y), (W, y)], fill=(8, 12, 28, a))
    canvas = Image.alpha_composite(canvas, veil)
    d = ImageDraw.Draw(canvas)
    cx, cy, r = W // 2, 300, 84
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(99, 102, 241, 235), outline=(255, 255, 255, 230), width=5)
    d.polygon([(cx - 30, cy - 48), (cx - 30, cy + 48), (cx + 52, cy)], fill=(255, 255, 255, 255))
    f = font("bold", 104)
    total = d.textlength("NEXUS AI", font=f)
    x = W // 2 - total / 2
    d.text((x, 640), "NEXUS", font=f, fill=TEXT)
    d.text((x + d.textlength("NEXUS ", font=f), 640), "AI", font=f, fill=ACCENT)
    d.text((W // 2, 815), "Enterprise RAG & Document Intelligence  ·  product demo", font=font("light", 46), fill=TEXT, anchor="mm")
    mins, secs = divmod(int(round(duration)), 60)
    d.text((W // 2, 900), f"Real application recording  ·  {mins}:{secs:02d}  ·  synthetic data", font=font("semibold", 30),
           fill=(199, 210, 254), anchor="mm")
    canvas.convert("RGB").save(path, optimize=True)


# ------------------------------------------------------------------------------------ timeline
@dataclass
class Seg:
    section: str
    caption: str
    start: tuple[str, float]
    end: tuple[str, float]
    speed: float = 1.0


SECTIONS = ["Single sign-on", "Document intelligence", "Human review", "Hybrid RAG", "Enterprise security",
            "Engineering & observability"]

TIMELINE: list[Seg] = [
    Seg("Single sign-on", "Login through Keycloak (OpenID Connect + PKCE); the browser only holds an HttpOnly session cookie",
        ("login_page:reviewer@northstar.demo", -0.6), ("logged_in:reviewer@northstar.demo", 3.2), 1.25),
    Seg("Document intelligence", "A reviewer uploads a synthetic Croatian invoice to private object storage",
        ("library", -0.4), ("upload_row_visible", 1.6), 1.15),
    Seg("Document intelligence", "A durable PostgreSQL job queue hands it to a worker: parse, extract, chunk, embed",
        ("upload_ready", -0.4), ("upload_ready", 4.8)),
    Seg("Document intelligence", "The lifecycle panel shows the real processing history of this upload",
        ("lifecycle", -0.6), ("lifecycle", 4.6)),
    Seg("Document intelligence", "Layout-aware parsing rebuilds the invoice table; every field is grounded in verbatim evidence",
        ("di_detail", -0.5), ("di_approved", -1.3)),
    Seg("Human review", "The reviewer approves the extraction; the decision is versioned in the review history",
        ("di_approved", -1.3), ("di_approved", 4.0)),
    Seg("Human review", "A scanned invoice: Tesseract OCR with deskew and table reconstruction",
        ("ocr_detail", -0.4), ("ocr_blocked", -0.9)),
    Seg("Human review", "Its totals do not add up, so business validation blocks the approval",
        ("ocr_blocked", -0.9), ("ocr_blocked", 5.2)),
    Seg("Hybrid RAG", "A Croatian question about the new invoice: PostgreSQL full-text + pgvector search, fused with RRF",
        ("assistant", -0.3), ("rag_answer", 0.4), 1.2),
    Seg("Hybrid RAG", "Top source [S1] is the invoice uploaded a minute ago, quoted verbatim with its page",
        ("rag_answer", 0.0), ("fusion_asked", -2.8)),
    Seg("Hybrid RAG", "Across contracts: every quote carries a citation plus its keyword / semantic rank and RRF score",
        ("fusion_asked", -1.0), ("blocked_asked", -3.5)),
    Seg("Enterprise security", "An identifier that exists only in another tenant: no evidence, nothing is cited",
        ("blocked_asked", -1.0), ("blocked_answer", 5.3)),
    Seg("Enterprise security", "Signing in as a user of a different tenant (Otherco)",
        ("keycloak:finance@otherco.demo", 0.0), ("logged_in:finance@otherco.demo", 0.8), 1.6),
    Seg("Enterprise security", "Otherco's library contains only its own document: tenant isolation plus row-level security",
        ("otherco_library", -0.3), ("otherco_library", 4.8)),
    Seg("Enterprise security", "Asking for the Northstar invoice from Otherco returns no evidence and no sources",
        ("otherco_asked", -0.6), ("otherco_answer", 5.6), 1.1),
    Seg("Engineering & observability", "Retrieval quality is measured on a held-out synthetic test set, not claimed",
        ("evaluation", -0.4), ("evaluation", 5.6)),
    Seg("Engineering & observability", "Prometheus + Grafana: live API, queue and worker metrics from this session",
        ("grafana", -0.5), ("end", 0.0)),
]


def load_recording(rec: Path) -> tuple[list[dict], dict[str, float]]:
    frames = json.loads((rec / "frames.json").read_text())
    frames.sort(key=lambda f: f["t"])
    markers = {m["name"]: m["t"] for m in json.loads((rec / "markers.json").read_text())}
    return frames, markers


def segment_clip(rec: Path, frames: list[dict], t0: float, t1: float, speed: float, overlay: Path, frame_png: Path,
                 out: Path, work: Path) -> float:
    before = [f for f in frames if f["t"] <= t0]
    inside = [f for f in frames if t0 < f["t"] < t1]
    seq = ([before[-1]] if before else []) + inside
    if not seq:
        raise SystemExit(f"no frames between {t0} and {t1}")
    lst = work / (out.stem + ".txt")
    lines = []
    for i, f in enumerate(seq):
        start = max(f["t"], t0)
        stop = seq[i + 1]["t"] if i + 1 < len(seq) else t1
        dur = max(stop - start, 0.001) / speed
        lines.append(f"file '{(rec / 'frames' / f['file']).as_posix()}'\nduration {dur:.4f}")
    lines.append(f"file '{(rec / 'frames' / seq[-1]['file']).as_posix()}'")
    lst.write_text("\n".join(lines) + "\n")
    length = (t1 - t0) / speed
    run(["-f", "concat", "-safe", "0", "-i", str(lst), "-loop", "1", "-i", str(frame_png), "-loop", "1", "-i", str(overlay),
         "-filter_complex",
         f"color=c=0x0a0e1e:s={W}x{H}:r={FPS}[bg];"
         f"[0:v]fps={FPS},scale={BW}:{BH}:flags=lanczos,setsar=1[app];"
         f"[bg][app]overlay={BX}:{BY}:shortest=1[a];[a][1:v]overlay=0:0:shortest=1[b];[b][2:v]overlay=0:0:shortest=1,scale=out_range=tv:out_color_matrix=bt709,format=yuv420p[v]",
         "-map", "[v]", "-t", f"{length:.3f}", "-r", str(FPS), "-c:v", "libx264", "-preset", "medium", "-crf", "12", str(out)])
    return length


def still_clip(png: Path, seconds: float, out: Path) -> float:
    run(["-loop", "1", "-i", str(png), "-t", f"{seconds:.3f}", "-r", str(FPS), "-vf", "scale=out_range=tv:out_color_matrix=bt709,format=yuv420p,setsar=1",
         "-c:v", "libx264", "-preset", "medium", "-crf", "12", "-tune", "stillimage", str(out)])
    return seconds


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("recording", type=Path)
    ap.add_argument("output", type=Path)
    ap.add_argument("--thumbnail", type=Path)
    ap.add_argument("--crf", type=int, default=18)
    args = ap.parse_args()
    rec = args.recording.resolve()
    work = rec / "build"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    frames, markers = load_recording(rec)

    frame_png = work / "frame.png"
    frame_overlay(frame_png)
    title_card(work / "title.png")
    closing_card(work / "closing.png")

    clips: list[tuple[Path, float]] = [(work / "c000_title.mp4", still_clip(work / "title.png", 6.5, work / "c000_title.mp4"))]
    for i, seg in enumerate(TIMELINE, start=1):
        if seg.start[0] not in markers or seg.end[0] not in markers:
            print(f"WARNING: skipping segment {i}: marker missing")
            continue
        t0 = markers[seg.start[0]] + seg.start[1]
        t1 = markers[seg.end[0]] + seg.end[1]
        overlay = work / f"cap{i:03d}.png"
        caption_overlay(overlay, SECTIONS.index(seg.section) + 1, seg.section, seg.caption)
        out = work / f"c{i:03d}.mp4"
        length = segment_clip(rec, frames, t0, t1, seg.speed, overlay, frame_png, out, work)
        print(f"{i:02d} {seg.section:<28} {length:5.1f}s  {seg.caption[:70]}")
        clips.append((out, length))
    clips.append((work / "c999_closing.mp4", still_clip(work / "closing.png", 7.5, work / "c999_closing.mp4")))

    # cross-fade chain
    inputs: list[str] = []
    for path, _ in clips:
        inputs += ["-i", str(path)]
    chain, offset, prev = [], 0.0, "[0:v]"
    for k in range(1, len(clips)):
        offset += clips[k - 1][1] - XFADE
        label = f"[x{k}]"
        chain.append(f"{prev}[{k}:v]xfade=transition=fade:duration={XFADE}:offset={offset:.3f}{label}")
        prev = label
    total = sum(c[1] for c in clips) - XFADE * (len(clips) - 1)
    chain.append(f"{prev}fade=t=in:st=0:d=0.6,fade=t=out:st={total - 0.8:.3f}:d=0.8,scale=out_range=tv:out_color_matrix=bt709,format=yuv420p[out]")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    run([*inputs, "-filter_complex", ";".join(chain), "-map", "[out]", "-r", str(FPS), "-c:v", "libx264", "-preset", "slow",
         "-crf", str(args.crf), "-profile:v", "high", "-pix_fmt", "yuv420p", "-color_range", "tv", "-colorspace", "bt709",
         "-color_primaries", "bt709", "-color_trc", "bt709", "-movflags", "+faststart",
         "-metadata", "title=NEXUS AI - Enterprise RAG & Document Intelligence demo", str(args.output)])
    print(f"wrote {args.output} ({total:.1f}s)")
    if args.thumbnail:
        t = markers["di_evidence"] + 0.3
        app_frame = rec / "frames" / [f for f in frames if f["t"] <= t][-1]["file"]
        thumbnail(args.thumbnail, app_frame, frame_png, total)
        print(f"wrote {args.thumbnail}")


if __name__ == "__main__":
    main()
