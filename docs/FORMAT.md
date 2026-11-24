# Clip format

A clip is a folder `clip_<YYYYMMDD>_<HHMMSS>` written by the capture DLL. Everything is little-endian.

## Files

| File | What |
|---|---|
| `clip.txt` | The upscale context's create line (`flags 0x.. maxRender WxH maxUpscale WxH`), the game's executable name, the capture DLL version |
| `frame_NNN.txt` | One frame's dispatch parameters (below) |
| `frame_NNN_<input>_<W>x<H>_f<F>.bin` | One input of one frame: `<W>` x `<H>` texels in the game's DXGI format `<F>`, rows packed (no row padding), top row first |

`<input>` is one of `color`, `depth`, `motion`, `exposure`, `reactive`, `tc` (transparency and composition). An input the game does not give is missing.
Frame numbers count recorded frames from 0.

A packed clip (made by `tools/clippack.py`, optional) has `frame_..._f<F>.bin.s<k>.zst` instead of each `.bin`, plus `PACKED.txt`
(name, size and SHA-256 of every original file): the file's bytes regrouped by their position within a `k`-byte texel (all first bytes, then all
second bytes, ...), compressed with zstd. `tools/cybocap.py` and `clippack.load()` read both kinds.

## Formats seen so far

| DXGI | Name | Bytes per texel | How `cybocap.py` reads it |
|---:|---|---:|---|
| 10 | R16G16B16A16_FLOAT | 8 | float32 RGBA |
| 16 | R32G32_FLOAT | 8 | float32 XY |
| 26 | R11G11B10_FLOAT | 4 | float32 RGB (each value is exactly an FP16 value) |
| 34 | R16G16_FLOAT | 4 | float32 XY |
| 40, 41 | D32_FLOAT, R32_FLOAT | 4 | float32 |
| 44, 45, 46 | R24G8 / D24_UNORM_S8 / R24_UNORM_X8 | 4 | float32 = low 24 bits / (2^24 - 1) |
| 61 | R8_UNORM | 1 | uint8 |

## frame_NNN.txt

```
frame <n> dispatch <game's dispatch count>
<input> <width>x<height> format <DXGI> state <FFX resource state> copied_rows <rows> row_bytes <bytes>   (one line per input)
jitter <x> <y>                       render pixels, as given to FSR
motionVectorScale <x> <y>            stored motion x this = render pixels
renderSize <w> <h>
upscaleSize <w> <h>
enableSharpening <0|1> sharpness <s>
frameTimeDelta <ms>
preExposure <value>
reset <0|1>
cameraNear <n> cameraFar <f> fovV <radians> viewSpaceToMeters <m>
flags <FSR dispatch flags>
```

## Create flags (clip.txt)

FSR's `FFX_UPSCALE_ENABLE_*` bits: 0x1 HDR color, 0x2 motion vectors at display size, 0x4 motion vectors include the jitter, 0x8 depth inverted
(near = 1), 0x10 infinite far plane, 0x20 auto exposure. Clips from capture versions before `clip.txt` existed need the flags given by hand
(`cybocap.py ... --flags 0x9`).
