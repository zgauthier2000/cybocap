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

## GPU-packed files (`*.bin.gp`, CyboCap 1.1 with `GpuPack=1`)

Color, depth and motion are packed on the GPU; `frame_..._f<F>.bin.gp` holds exactly the bytes the `.bin` would have held, losslessly coded.
32-bit little-endian words:

| Word | What |
|---|---|
| 0 | Words in the file |
| 1 | Magic `0x50475943` ("CYGP") |
| 2 | Version (1) |
| 3, 4 | Width, height |
| 5 | Layout: 0 R11G11B10 (channels 11/11/10 bits), 1 R24G8 / D24S8 (24 + 8), 2 RG32F, 3 R32F / D32F, 4 RGBA16F, 5 RG16F |
| 6, 7 | Tiles across, tiles down (8x8 texel tiles) |
| 8 ... | One word per tile (row-major): the word where that tile's record starts |
| then | Tile records, in any order |

**Values.** Each channel is an integer of its width: the stored bits for layouts 0 and 1; for floats (2 to 5) the bits mapped to an
order-preserving integer (negative: all bits flipped; otherwise the sign bit set).

**Residuals.** Inside each 8-row tile row, value minus (left + above - above-left); in the tile row's first row, value minus left (texels
outside the image count as 0). Taken modulo 2^width as the integer nearest 0, then zigzagged ((r << 1) ^ (r >> 31)).

**Tile record.** Word 0: per channel one byte, `b | 0x80` if the channel uses flags. Word 1: per channel one byte, `B - b` (B = bits of the
tile's largest residual). Then for each channel in turn:
- 64 fields in texel order (row-major), each `b + f` bits (f = 1 with flags), least significant bit first, packed into `2 * (b + f)` words:
  the residual's low `b` bits, and above them the flag (set when the residual needs more than `b` bits);
- with flags: for each flagged texel in order, its high `B - b` bits, packed the same way into `ceil(flagged * (B - b) / 32)` words.

`tools/clippack.py` (`gp_decode`) is the reference decoder.

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
