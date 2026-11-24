# CyboCap

> **Who this is for**
>
> - **Developers** working on upscalers, denoisers or frame generation who want real game inputs for their own testing and training.
>   Record clips and use them however you like; nothing is sent anywhere.
> - **Testers** who want to help Cyboman's upscaler work with detailed data from a specific game or scene: a place where an upscaler
>   shimmers, smears or ghosts, a game that is not covered yet, or a scene worth testing. Record a short clip and share the link with one
>   command ([Sharing a capture](#sharing-a-capture-with-us)). Sharing is always your choice.
>
> CyboCap is a capture tool, not something that improves the picture or performance while you play: leave it out of your game folder when
> you are not recording.

Records what a game hands to AMD FSR each frame (the color image before upscaling, depth, motion vectors, exposure, reactive and
transparency masks, and every dispatch parameter) as clips on disk, through [OptiScaler](https://github.com/optiscaler/OptiScaler)'s FSR
upscaler slot. Made for testing and training upscalers on real game footage. Today it captures FSR's inputs (including DLSS and XeSS
inputs that OptiScaler hands to FSR); capturing other upscalers' inputs directly may come later, with the same clip format.

- **FSR capture DLL** (`amd_fidelityfx_upscaler_dx12.dll`): a small DLL that sits in front of FSR's upscaler DLL, passes the game's calls
  through, and copies the inputs while you record. By default it runs FSR as FSR 3.1.x, the fastest version in the FSR DLL on any GPU,
  so recording costs as little as possible on slower PCs (see `FastUpscaler` below). The clips do not depend on which FSR version runs.
- **tools/cybocap.py**: reads clips and exports them (NumPy, EXR, PNG previews, MP4, a per-frame settings table, or an nr-record recording).
- **tools/clippack.py**: optional lossless packing, about half the size.

**Status:** this version is tested under Proton on Linux (RX 7800 XT) in Shadow of the Tomb Raider; earlier versions recorded clips in
Cyberpunk 2077, Stellar Blade and the Black Myth: Wukong benchmark the same way. **Windows is untested**: reports welcome (issue with your GPU, game, and `cybocap.log`).

## What it records, and what it does not

- D3D12 games, through OptiScaler's FSR 3.1 / FSR 4 path (the game's own FSR, or DLSS / XeSS inputs that OptiScaler hands to FSR).
- The inputs only, at render resolution (motion vectors at whatever size the game gives). It does **not** record the upscaled output.
- No Vulkan, no D3D11.
- **Known limitation, `FastUpscaler`:** the clips hold what the game sends before FSR runs, so the FSR version normally does not change them.
  In Shadow of the Tomb Raider we checked this with an A/B recording (FSR 3.1.5 against a FSR 4-based build): every setting was identical
  and the picture statistics matched within normal run-to-run variation. A game whose engine reuses the previous upscaled frame (for
  example in reflections or auto exposure) could still differ slightly, pixel by pixel; set `FastUpscaler=0` for such a game.

## CyboCap and Apex Compute's nr-record

[nr-record](https://github.com/apex-compute/ANR) (part of Apex Compute's ANR project) also records game inputs through OptiScaler. The two
were built for different jobs. This table is from nr-record's own documentation and our use of both; it is not a same-moment benchmark.

| | CyboCap | nr-record |
|---|---|---|
| Where it hooks in | In front of FSR's DLL, in OptiScaler's FSR slot | OptiScaler's custom provider slots; forwards to a real upscaler |
| Inputs it sees | FSR's inputs (DLSS / XeSS games: after OptiScaler's translation to FSR) | What the game gives DLSS, FSR or XeSS; also frame generation and ray reconstruction inputs |
| Graphics APIs | D3D12 | D3D12 and Vulkan |
| Upscaled output | Not recorded | Recorded |
| Storage | Plain files, uncompressed (about 46 MB per frame at 2258x1270); optional lossless packing afterwards (about half) | Coded losslessly on the GPU while recording, then zstd (their docs: about 26 MB per real frame at 4K for frame generation inputs) |
| When the disk falls behind | The game never loses a frame: short clips fit in memory; Shift+F10 ends the clip one frame before the game would wait. Every clip is an unbroken run | By design, frames are dropped and the gap is marked, so the game is never held up |
| Recording | F10 / Shift+F10, fixed or longest-possible clips | Start / stop key, records until stopped |
| Reading recordings | One file per input and frame plus a text file of parameters; `cybocap.py` exports NumPy, EXR, PNG, MP4, CSV / JSON, and nr-record | Their own binary format and reader; replay through real upscaler DLLs; a side-by-side viewer |

In short: nr-record covers more (APIs, features, the upscaled output, replay and viewing); CyboCap aims at gap-free clips in a simple format
that any language can read. `cybocap.py nrrecord` converts a CyboCap clip into an nr-record recording, so their replay and viewer can use it
(checked with our own decoder, not with their tools).

## Install

1. Set the game up with OptiScaler and an FSR 3.1 or FSR 4 upscaler as usual, and check it runs.
2. In the folder where OptiScaler loads `amd_fidelityfx_upscaler_dx12.dll` from, rename that file to `amd_fidelityfx_upscaler_dx12_real.dll`.
3. Copy this project's `amd_fidelityfx_upscaler_dx12.dll` there, and `cybocap.ini.example` as `cybocap.ini`.
4. Start the game. `cybocap.log` appears in the clip folder (by default `clips` next to the DLL).

To remove it: delete the capture DLL and rename `_real` back.

**Online games with anti-cheat:** do not use it there. Like OptiScaler itself, a DLL in front of another DLL can be flagged.

## Recording

| Key | Clip |
|---|---|
| F10 | `Seconds` long (default 2 s) |
| Shift+F10 | As long as possible without slowing the game, up to `LongSeconds` (default 20 s) |

Clips are written while you play. A second press while the last clip is still being written is ignored (see the log).

**Why 2 seconds, and what Shift+F10 does.** A frame of inputs is large: 44 to 73 MB uncompressed (about 46 MB at 2258x1270), so 60 FPS
needs about 2.7 GB/s of disk writes. The DLL keeps up to 128 frames in memory and writes them in the background, so a short clip never slows
the game. Longer clips depend on the drive: in our tests NVMe drives wrote 2 to 2.4 GB/s at first and 0.8 to 1.1 GB/s once their fast write
cache ran out. Shift+F10 records until the memory buffer is about to run out and stops one frame before the game would have to wait
(6.5 s at 60 FPS in one test). Set `LongStopWhenFull=0` to always record `LongSeconds` and accept the slowdown.

**Memory:** while a clip is recorded, about 6 to 9 GB of system RAM (128 frames). Lower `Slots` on a 16 GB PC.

**The first press** after starting the game sets the memory aside (about 1.5 s pause); that clip starts a few frames later so the pause is
not in it.

## Settings (cybocap.ini)

| Key | Default | What |
|---|---|---|
| `Seconds` | 2 | F10 clip length |
| `LongSeconds` | 20 | Shift+F10 longest clip |
| `LongStopWhenFull` | 1 | Shift+F10 stops before the game would wait |
| `FastUpscaler` | 1 | Run FSR as FSR 3.1.x while CyboCap is installed (fastest; lower picture quality than FSR 4). 0 = the version OptiScaler picks |
| `Writers` | 1 | Writer threads (1-4); more helped only on drives with write cache left |
| `Slots` | 128 | Frames held in memory (2-128) |
| `Folder` | empty | Where clips go; empty = `clips` next to the DLL |

Environment variables `CYBOCAP_DIR`, `CYBOCAP_FRAMES`, `CYBOCAP_LONG_FRAMES`, `CYBOCAP_WRITERS`, `CYBOCAP_SLOTS` override the file.

## Reading clips

```
pip install -r requirements.txt          # numpy, zstandard, Pillow; ffmpeg for --mp4
python tools/cybocap.py info clips/clip_20261124_075746
python tools/cybocap.py export clips/clip_20261124_075746 out              # .npy per input and frame + frames.json + frames.csv
python tools/cybocap.py export clips/clip_20261124_075746 out --exr --png --mp4 --frames 0:60
python tools/cybocap.py nrrecord clips/clip_20261124_075746 rec            # nr-record-4 recording
```

Values: color is the game's linear HDR color before upscaling (pre-exposed by `pre_exposure`); depth as the game stores it (`depth_inverted`
says whether near is 1); motion as stored, and `motion_px` = motion x `motion_scale` in render pixels. The PNG previews use a simple
x/(1+x) tone curve, not the game's tone mapping. File layout: [docs/FORMAT.md](docs/FORMAT.md).

The nr-record export follows the format description Apex Compute publishes for their nr-record tools
([github.com/apex-compute/ANR](https://github.com/apex-compute/ANR), `record/FORMAT.md`); it was checked with our own decoder, not with theirs.

## Sharing a capture with us

```
python tools/cybocap.py share clips/clip_20261124_075746
```

1. Shows the terms below and asks you to type `yes`.
2. Packs the clip losslessly into one file, `clip_<time>_<game>.cybocap.tar` (about half the size; your original clip is kept).
3. You upload that file wherever you like (Google Drive, MEGA, Dropbox, your own server, ...) with a link that downloads without signing in,
   and paste the link when asked (or later: `share <clip> --agree --link <url>`).
4. When you say yes, it sends the link and the clip's details (game, frames, sizes, file size, SHA-256) to Cyboman. Otherwise it prints the
   message to send yourself, plus a link that opens a pre-filled GitHub issue.

Terms: you upload the file yourself and you are responsible for that upload and the link you share. Only share captures you are allowed to
share (check the game's terms). We use shared captures to test and train upscalers; we do not republish your raw capture, and pictures made
from it may appear in comparisons. The file holds the game's images and settings and the game's executable name, no user names or paths.

## Packing (optional)

```
python tools/clippack.py pack clips/clip_20261124_075746            # writes clip_..._075746.packed, original kept
python tools/clippack.py pack clips/clip_20261124_075746 --replace  # deletes the original after every file is checked
python tools/clippack.py unpack clips/clip_..._075746.packed restored
```

Lossless (every file is decoded and compared before anything is deleted). Measured on our game clips: 20 to 51% of the original size. Takes about
as long as copying the clip twice. Nothing is packed unless you run it.

## Building

`MINGW=<llvm-mingw>/bin ./build.sh` with [llvm-mingw](https://github.com/mstorsjo/llvm-mingw). The FidelityFX SDK headers in
`third_party/ffx` are AMD's (MIT, see their NOTICE).

## License

GNU General Public License v2 or later (see `LICENSE`), the same as the [FSR 4 shader project](https://github.com/zgauthier2000/fsr4-shader-performance-overrides).
The AMD FidelityFX SDK headers in `third_party/ffx` keep their MIT license.
