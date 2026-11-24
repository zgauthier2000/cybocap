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
  Since 1.1 it also packs color, depth and motion losslessly **on the GPU** before saving them (`GpuPack`, on by default): in our tests
  clips were about a third of their raw size, so the drive keeps up much longer.
- **tools/cybocap.py**: reads clips and exports them (NumPy, EXR, PNG previews, MP4, a per-frame settings table, or an nr-record recording).
- **tools/clippack.py**: optional lossless packing of raw clips (about half the size), and the decoder for GPU-packed files.

## Space savings

**CyboCap 1.1 against 1.0** (Shadow of the Tomb Raider, 2258x1270 render size, RX 7800 XT):

| Input | 1.0 (plain files) | 1.1 (GPU packing) |
|---|---|---|
| Color (R11G11B10) | 11.5 MB | 6.0 MB (53%) |
| Depth (R24G8) | 11.5 MB | 3.5 MB (30%) |
| Motion vectors (RG32F) | 22.9 MB | 7.0 MB (31%) |
| **Per frame** | **45.9 MB** | **16.5 MB (36%)** |
| A 10-second clip at 60 FPS | about 28 GB | about 10 GB |

**CyboCap 1.1 against Apex Compute's nr-record, on the same frames.** We took an nr-record recording of the Black Myth: Wukong benchmark,
decoded its inputs and fed exactly those textures to CyboCap 1.1 in our test harness. Both pack losslessly on the GPU; sizes per frame:

| Input | Raw | nr-record | CyboCap 1.1 |
|---|---|---|---|
| Color (2228x1256, RGBA16F) | 22.4 MB | 10.8 MB (48%) | 11.2 MB (50%) |
| Depth (2228x1256, 32-bit) | 11.2 MB | 4.2 MB (37%) | 4.6 MB (41%) |
| Motion vectors (3840x2160, RG16F) | 33.2 MB | 4.1 MB (12%) | 5.5 MB (17%) |
| **These three inputs** | **66.8 MB** | **19.0 MB (28%)** | **21.3 MB (32%)** |

nr-record packs these inputs about 11% smaller than CyboCap; both bring them to about a third of their raw size. nr-record's frames also
hold the upscaled output (another 26 MB per frame in this recording), which CyboCap does not record. nr-record's sizes are from its own
recording (82 frames); CyboCap's are over 53 of the same frames.

**Status:** this version is tested under Proton on Linux (RX 7800 XT) in Shadow of the Tomb Raider (GPU packing checked byte for byte there); earlier versions recorded clips in
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
| Storage | Packed losslessly on the GPU while recording (since 1.1), or plain files with `GpuPack=0`. Same Wukong inputs: 21.3 MB per frame (32% of raw) | Coded losslessly on the GPU while recording, then zstd. Same Wukong inputs: 19.0 MB per frame (28% of raw), plus the upscaled output (see [Space savings](#space-savings)) |
| When the disk falls behind | The game never loses a frame: short clips fit in memory; Shift+F10 ends the clip one frame before the game would wait. Every clip is an unbroken run | By design, frames are dropped and the gap is marked, so the game is never held up |
| Recording | F10 / Shift+F10, fixed or longest-possible clips | Start / stop key, records until stopped |
| Reading recordings | One file per input and frame plus a text file of parameters; `cybocap.py` exports NumPy, EXR, PNG, MP4, CSV / JSON, and nr-record | Their own binary format and reader; replay through real upscaler DLLs; a side-by-side viewer |

In short: nr-record covers more (APIs, features, the upscaled output, replay and viewing); CyboCap aims at gap-free clips in a simple format
that any language can read. `cybocap.py nrrecord` converts a CyboCap clip into an nr-record recording, so their replay and viewer can use it
(checked with our own decoder, not with their tools).

## Prerequisites

**To record (the DLL)**

| | |
|---|---|
| Operating system | Windows 10 or 11, 64-bit; or Linux with Proton / Wine (where it was tested) |
| Game | A D3D12 game that OptiScaler can run with an FSR 3.1 or FSR 4 upscaler (the game's own FSR, or its DLSS / XeSS inputs translated by OptiScaler) |
| OptiScaler | A current release ([OptiScaler](https://github.com/optiscaler/OptiScaler)), set up for the game and working **before** you add CyboCap. Tested with OptiScaler 10.0 (development build) |
| FSR DLLs | OptiScaler's folder must hold AMD's FidelityFX loader and upscaler DLLs: `amd_fidelityfx_loader_dx12.dll` and `amd_fidelityfx_upscaler_dx12.dll` (FidelityFX SDK 2.x, as OptiScaler uses for FSR 3.1.4 and later). CyboCap goes in front of the upscaler DLL |
| GPU | Any GPU that runs FSR 3.1 in D3D12 (AMD, NVIDIA, Intel). With `FastUpscaler=1` (default) FSR runs as FSR 3.1.x, so FSR 4 support is not needed |
| Memory | 16 GB RAM or more; 32 GB recommended. While a clip is recorded CyboCap holds up to 128 frames in RAM (about 4 to 9 GB, depending on the render size); on 16 GB set `Slots=48` or lower |
| Disk | An SSD, ideally NVMe, with room for clips: about 2 GB per 2-second clip at a 2258x1270 render size with GPU packing (5.5 GB uncompressed). Long clips need a fast drive (see Recording) |

The DLL is built as a single file with its runtime included: no Visual C++ redistributable or other install is needed.

**Windows notes**
- **Antivirus:** a DLL that stands in for another DLL can be flagged or quarantined. If the game stops loading FSR after you add CyboCap, check your antivirus history. Only add an exclusion for the game folder if you trust the download (check the SHA-256 on the release page).
- **Anti-cheat:** do not use CyboCap (or OptiScaler) in online games with anti-cheat.
- **Folder rights:** games under `C:\Program Files` may block writing clips next to the DLL. Set `Folder=` in `cybocap.ini` to a folder you can write to, for example `Folder=D:\CyboCap\clips`.
- **Windows is untested so far** (the DLL has only run under Proton on Linux). Reports are welcome as issues: your GPU, the game, and `cybocap.log`.

**To read, export, pack or share clips (the Python tools)**

| | Windows | Linux |
|---|---|---|
| Python | 3.10 or newer from [python.org](https://www.python.org/downloads/) (tested with 3.12). The `py` launcher it installs runs the tools: `py tools\cybocap.py info <clip>` | Your distribution's `python3` (3.10 or newer) |
| Packages | `py -m pip install -r requirements.txt` (numpy, zstandard, Pillow) | `python3 -m pip install -r requirements.txt`, or the distribution's packages |
| ffmpeg (only for `--mp4`) | `winget install Gyan.FFmpeg`, or a build from [ffmpeg.org](https://ffmpeg.org/download.html) on your PATH | The distribution's `ffmpeg` package |
| Space | Exports and packing need free space next to the clip: up to the clip's size again | same |

The tools were run on Windows 11 with Python 3.12 (every command; MP4 export not tested there).

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
| F10 or Shift+F10 **during** a clip | Ends the clip early; the frames recorded so far are saved as a normal clip (since 1.2) |

Clips are written while you play. A press after a clip has ended but while it is still being written is ignored (see the log).

**Why 2 seconds, and what Shift+F10 does.** A frame of inputs is large: 44 to 73 MB uncompressed (about 46 MB at 2258x1270), so 60 FPS
needs about 2.7 GB/s of disk writes. The DLL keeps up to 128 frames in memory and writes them in the background, so a short clip never slows
the game. Longer clips depend on the drive: in our tests NVMe drives wrote 2 to 2.4 GB/s at first and 0.8 to 1.1 GB/s once their fast write
cache ran out. Shift+F10 records until the memory buffer is about to run out and stops one frame before the game would have to wait.
Set `LongStopWhenFull=0` to always record `LongSeconds` and accept the slowdown.

**GPU packing (`GpuPack=1`, default since 1.1).** Before the frames leave the GPU, a compute shader packs color, depth and motion losslessly
(each texel predicted from its neighbors, each 8x8 tile stored with only the bits it needs). In Shadow of the Tomb Raider on an RX 7800 XT:

| | GpuPack=0 (1.0) | GpuPack=1 (1.1) |
|---|---|---|
| Size per frame (2258x1270) | 45.9 MB | 16.5 MB (36%; color 53%, depth 30%, motion 31%) |
| Longest Shift+F10 clip we recorded | 6.5 s (fresh drive) | 11.1 s (the same drive after heavy use, writing only about 0.55 GB/s) |
| Frames over 25 ms during a clip | 0 | 0 |

Every packed file was checked against a raw copy of the same frame (366 files, all identical). The packing costs some GPU time on each
recorded frame: on the 7800 XT it did not show in the frame times; on slower GPUs it is untested (set `GpuPack=0` if recording feels rough).
Memory use is the same as without packing (the buffers are sized for the worst case). Packed files end in `.bin.gp`; the tools read them
like the raw ones.

**Faster drives.** The defaults are set for drives like ours (2 to 2.4 GB/s at first). On a faster PCIe 4.0 / 5.0 NVMe, try in `cybocap.ini`:

| Setting | Try | Why |
|---|---|---|
| `Writers` | 2 to 4 | More write streams. In our tests 2 to 4 streams wrote 8 to 18% faster on a drive with fast write cache left, and slower on one without, so check the log |
| `LongSeconds` | 30, 60 | Shift+F10's cap. Shift+F10 still stops before the game would wait, so a higher cap only lets a fast drive record longer |
| `Seconds` | 4, 5, ... | F10's length. Unlike Shift+F10, F10 does not stop early: raise it only if your drive keeps up, or the game slows down for the rest of the clip |
| `Slots` | 128 (the most) | Frames held in memory while the disk catches up; more RAM means longer clips at full speed |
| `GpuPack` | 1 (keep) | Packed frames are about a third the size, so any drive keeps up longer |

How to tell: `cybocap.log` ends each clip with `game waited N ms for free slots`. 0 ms means the drive kept up; for Shift+F10, the
`ring full at frame N` line shows how far it got. Keep the clips folder on the fast drive (`Folder=`).

**Memory:** while a clip is recorded, about 4 to 9 GB of system RAM (128 frames, depending on the render size). Lower `Slots` on a 16 GB PC.

**The first press** after starting the game sets the memory aside (about 1.5 s pause); that clip starts a few frames later so the pause is
not in it.

## Settings (cybocap.ini)

| Key | Default | What |
|---|---|---|
| `Seconds` | 2 | F10 clip length |
| `LongSeconds` | 20 | Shift+F10 longest clip |
| `LongStopWhenFull` | 1 | Shift+F10 stops before the game would wait |
| `FastUpscaler` | 1 | Run FSR as FSR 3.1.x while CyboCap is installed (fastest; lower picture quality than FSR 4). 0 = the version OptiScaler picks |
| `GpuPack` | 1 | Pack color, depth and motion losslessly on the GPU before saving (about a third the size). 0 = plain files |
| `GpuPackVerify` | 0 | Tests only: also save raw copies of the packed frames (more memory and disk), to check the packing in your game |
| `Writers` | 1 | Writer threads (1-4); more helped only on drives with write cache left |
| `Slots` | 128 | Frames held in memory (2-128) |
| `Folder` | empty | Where clips go; empty = `clips` next to the DLL |

Environment variables `CYBOCAP_DIR`, `CYBOCAP_FRAMES`, `CYBOCAP_LONG_FRAMES`, `CYBOCAP_WRITERS`, `CYBOCAP_SLOTS`, `CYBOCAP_FAST_UPSCALER`,
`CYBOCAP_GPU_PACK` override the file.

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
x/(1+x) tone curve, not the game's tone mapping. File layout: [docs/FORMAT.md](docs/FORMAT.md). GPU-packed files are decoded on the fly
(about 1 second per frame in Python), so exports of long clips take a while.

The nr-record export follows the format description Apex Compute publishes for their nr-record tools
([github.com/apex-compute/ANR](https://github.com/apex-compute/ANR), `record/FORMAT.md`); it was checked with our own decoder, not with theirs.

## Sharing a capture with us

```
python tools/cybocap.py share clips/clip_20261124_075746
```

1. Shows the terms below and asks you to type `yes`.
2. Puts the clip into one file, `clip_<time>_<game>.cybocap.tar` (raw files packed losslessly to about half; GPU-packed files are already
   small; your original clip is kept).
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

For raw clips (`GpuPack=0`, or clips from 1.0). Lossless (every file is decoded and compared before anything is deleted). Measured on our
game clips: 20 to 51% of the original size. Takes about as long as copying the clip twice. Nothing is packed unless you run it. GPU-packed
files (`.bin.gp`) are left as they are.

## Building

`MINGW=<llvm-mingw>/bin ./build.sh` with [llvm-mingw](https://github.com/mstorsjo/llvm-mingw). The GPU packing shader
(`src/shaders/gpupack.hlsl`) is compiled to signed DXIL with Microsoft's [DirectX Shader Compiler](https://github.com/microsoft/DirectXShaderCompiler)
(`dxc -T cs_6_0 -E main -O3 -Fh gpupack_dxil.h -Vn g_gpupack gpupack.hlsl`); the result is checked in. The FidelityFX SDK headers in
`third_party/ffx` are AMD's (MIT, see their NOTICE).

## License

GNU General Public License v2 or later (see `LICENSE`), the same as the [FSR 4 shader project](https://github.com/zgauthier2000/fsr4-shader-performance-overrides).
The AMD FidelityFX SDK headers in `third_party/ffx` keep their MIT license.
