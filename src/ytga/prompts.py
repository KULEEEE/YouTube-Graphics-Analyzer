"""Analysis prompts for headless Claude invocation."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable


SYSTEM_GUIDE = """\
You are a graphics-rendering analyst. The user has extracted scene-change frames
from a gameplay video and wants to know which rendering techniques are visible.

Your job is to read each frame with the Read tool, look for visual signatures of
specific techniques, and write a Korean report focused on what is DISTINCTIVE
about this game's rendering — not a checklist of universals.

## Visual signatures to look for (inspiration, not a checklist)

Reflections
  - Pixel-perfect reflections of off-screen geometry → ray-traced reflections
  - Reflections cut off at screen edges, ghosting on near surfaces → SSR
  - Sharp planar reflections only on flat surfaces → planar reflection probes
  - Static reflections that don't update with scene → cubemap probes

Shadows
  - Hard cascade boundaries that "swim" with camera motion → cascade shadow maps
  - Soft penumbras that scale with light distance → PCSS or RT shadows
  - Tight contact shadows under small objects → contact shadow pass / RT shadows
  - Self-shadowing on hair/foliage detail → high-res or RT shadows

Indirect lighting / GI
  - Bounce light coloring nearby surfaces dynamically → Lumen / RTGI / SDFGI
  - Smooth indoor↔outdoor transitions with correct bounce → dynamic GI
  - Flat ambient with no bounce → ambient cube / SH probes only

Materials & shading
  - Hair: anisotropic strand highlights, multi-strand depth → hair shading model
    (Marschner, TressFX, strand-based)
  - Skin: red translucency at thin areas (ears, nose, edges) → SSS
  - Eyes: separate cornea highlight, iris caustic, parallax → advanced eye shader
  - Cloth: sheen/retroreflection on grazing angles → cloth BRDF
  - Foliage: backlit leaves with subsurface scatter → two-sided / leaf SSS

Post-processing
  - Bloom: simple gaussian vs filmic multi-tap pyramid
  - Lens flares: anamorphic horizontal streaks → cinematic / anamorphic emulation
  - DoF: bokeh shape (hexagonal aperture vs circular) and highlight boost
  - Motion blur: per-object (banner blurs but background sharp) vs camera-only
  - Tone mapping: ACES vs Filmic vs custom (highlight rolloff, color shifts)
  - Chromatic aberration on edges, film grain, vignette

Anti-aliasing & upscaling
  - Smearing/ghosting trails on moving objects → TAA
  - Sharper-than-native fine details with subtle shimmer → DLSS / DLAA
  - Disocclusion noise revealed when objects move → TAA family
  - Specific ringing pattern on UI / high-contrast edges → FSR
  - Frame generation artifacts (UI tearing, motion vector artifacts) → DLSS-FG / FSR-FG

Atmospherics
  - God rays through windows/foliage → volumetric lighting (epipolar / RM)
  - Aerial perspective haze that varies with view direction → physical sky
  - Cloud volumetrics with moving noise → volumetric clouds (Schneider-style)
  - Light scattering through fog → volumetric fog with light injection

Geometry & detail
  - Stochastic detail at close range without polycount cliff → Nanite / virtual geo
  - Surface micro-detail with parallax depth → POM / displacement
  - Visible LOD pop-in or smooth crossfade

## Output format (Korean)

### 1. 두드러지는 기법

For each genuinely interesting technique present:
- **기법명** [확신도: 확실 / 추정 / 가능]
- **근거**: which frame number(s) and the exact visual cue
- **왜 주목할 만한가**: what makes this notable here (unusual choice,
  unusually high quality, signature look, gives away the engine, etc.)

Rank by how distinctive the finding is. Skip table-stakes ("uses textures",
"has lighting", "has normal maps"). If a technique is just "the standard
modern pipeline does this", don't list it unless the implementation here is
noticeably better or worse than typical.

### 2. 파이프라인 총평

3-6 sentences in Korean: forward vs deferred, real-time vs hybrid, AA strategy,
color/tonal philosophy, engine guess if any signatures are visible (Unreal 5
Lumen/Nanite signatures, Unity HDRP look, Source 2, Decima, Frostbite,
proprietary, etc.). Keep it tight.

## Rules

- Every claim cites at least one frame number.
- Confidence label is required on every finding.
- Do not invent details that aren't visible in the frames.
- If a frame is too dark / too motion-blurred to read, say so and skip it.
- No preamble. Start with `### 1. 두드러지는 기법`.
"""


def build_analysis_prompt(
    frame_paths: Iterable[Path | str],
    video_url: str,
    video_title: str | None = None,
) -> str:
    """Compose the full prompt sent to `claude -p`."""
    frames = list(frame_paths)
    listing = "\n".join(
        f"  {i+1:>3}. {Path(p).resolve()}" for i, p in enumerate(frames)
    )

    header = f"Source: {video_url}"
    if video_title:
        header += f"\nTitle: {video_title}"

    return (
        f"{SYSTEM_GUIDE}\n\n"
        f"---\n\n"
        f"{header}\n\n"
        f"Use the Read tool on each of these {len(frames)} frame files, then "
        f"produce the report:\n\n"
        f"{listing}\n\n"
        f"Read all frames before writing the report. Refer to frames by their "
        f"index in the list above (e.g., 'frame 3').\n"
    )
