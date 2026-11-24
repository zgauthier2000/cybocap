#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# cybocap.py: reads cybocap clips (plain, or packed by clippack.py) and exports them.
#
#   cybocap.py info <clip>                       what the clip holds
#   cybocap.py export <clip> <out> [--npy] [--exr] [--png] [--mp4] [--frames A:B] [--inputs color,depth,...] [--crop x,y,w,h]
#       default --npy: <out>/<input>_NNN.npy (exact values) + frames.json + frames.csv (every frame's settings)
#       --exr: 32-bit float EXR (color RGB, depth Z, motion R/G); --png: 8-bit previews (color tone-mapped x/(1+x), depth gray = larger stored value
#       brighter, motion as a color wheel); --mp4: H.264 video of the color previews (needs ffmpeg)
#   cybocap.py nrrecord <clip> <out dir> [--frames A:B]
#       an nr-record-4 super resolution recording (record.json, frames.idx, frames.bin, params.bin; coding 3), written from the format description
#       published by Apex Compute (github.com/apex-compute/ANR, record/FORMAT.md), for tools that read that format
#   cybocap.py share <clip> [out dir] [--link URL] [--agree] [--send] [--open]
#       packs the clip (lossless; the original is kept) into ONE file <clip>_<game>.cybocap.tar to upload wherever you like, then takes the download
#       link and sends it to Cyboman when you say yes (--send), or prints the message plus a pre-filled GitHub issue link.
#       You upload and share the file yourself.
#   --flags 0x..: the FSR create flags for clips recorded before clip.txt existed
#
# Values: color = the game's linear HDR color before FSR (float32 RGB); depth = as the game stores it (float32; 24-bit depth as value / (2^24 - 1));
# motion = float32 X/Y as stored, motion_px = motion * motion_scale (render pixels); exposure = one float per frame; reactive / tc = uint8.
# Needs: numpy, zstandard, Pillow (for --png / --mp4), ffmpeg (for --mp4).
import sys, os, re, glob, json, csv, struct, zlib, time, argparse, subprocess
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from clippack import load

FILE = re.compile(r'frame_(\d+)_([a-z]+)_(\d+)x(\d+)_f(\d+)\.bin(?:\.s\d+\.zst)?$')
# DXGI format -> (bytes per texel, short name)
FMT = {2: (16, 'RGBA32F'), 10: (8, 'RGBA16F'), 16: (8, 'RG32F'), 19: (8, 'R32G8X24'), 20: (8, 'D32S8X24'), 24: (4, 'RGB10A2'), 26: (4, 'R11G11B10F'),
       28: (4, 'RGBA8'), 34: (4, 'RG16F'), 39: (4, 'R32 typeless'), 40: (4, 'D32F'), 41: (4, 'R32F'), 44: (4, 'R24G8'), 45: (4, 'D24S8'), 46: (4, 'R24X8'),
       49: (2, 'RG8'), 54: (2, 'R16F'), 56: (2, 'R16'), 61: (1, 'R8'), 62: (1, 'R8UI'), 87: (4, 'BGRA8')}

def r11g11b10(u):
    """packed R11G11B10 floats -> float32 (H, W, 3); every value is exactly an FP16 value"""
    def f(v, mbits):
        e = (v >> mbits) & 31; m = v & ((1 << mbits) - 1)
        return np.where(e == 0, m / (1 << mbits) * 2.0 ** -14, np.where(e == 31, np.inf, (1 + m / (1 << mbits)) * 2.0 ** (e.astype(np.int64) - 15))).astype(np.float32)
    return np.stack([f(u & 0x7FF, 6), f((u >> 11) & 0x7FF, 6), f((u >> 22) & 0x3FF, 5)], -1)

def r11g11b10_half(u):
    """packed R11G11B10 -> FP16 bit patterns (H, W, 3): same exponent bias 15, mantissa shifted left, sign 0"""
    return np.stack([((u & 0x7FF) << 4), (((u >> 11) & 0x7FF) << 4), (((u >> 22) & 0x3FF) << 5)], -1).astype(np.uint16)

def decode(raw, w, h, fmt):
    """file bytes -> numpy array of the stored values (see the header)"""
    if fmt == 26: return r11g11b10(np.frombuffer(raw, np.uint32).reshape(h, w))
    if fmt == 10: return np.frombuffer(raw, np.float16).reshape(h, w, 4).astype(np.float32)
    if fmt == 2: return np.frombuffer(raw, np.float32).reshape(h, w, 4).copy()
    if fmt == 16: return np.frombuffer(raw, np.float32).reshape(h, w, 2).copy()
    if fmt == 34: return np.frombuffer(raw, np.float16).reshape(h, w, 2).astype(np.float32)
    if fmt in (39, 40, 41): return np.frombuffer(raw, np.float32).reshape(h, w).copy()
    if fmt in (19, 20): return np.frombuffer(raw, np.float32).reshape(h, w, 2)[..., 0].copy()
    if fmt in (44, 45, 46): return ((np.frombuffer(raw, np.uint32).reshape(h, w) & 0xFFFFFF) / 16777215.0).astype(np.float32)
    if fmt == 54: return np.frombuffer(raw, np.float16).reshape(h, w).astype(np.float32)
    if fmt in (61, 62): return np.frombuffer(raw, np.uint8).reshape(h, w).copy()
    if fmt == 49: return np.frombuffer(raw, np.uint8).reshape(h, w, 2).copy()
    if fmt in (28, 87): a = np.frombuffer(raw, np.uint8).reshape(h, w, 4).copy(); return a[..., [2, 1, 0, 3]] if fmt == 87 else a
    print(f'  format {fmt} not known: kept as raw bytes', file=sys.stderr); return np.frombuffer(raw, np.uint8).copy()

class Clip:
    def __init__(self, path, flags=None):
        self.path = path.rstrip('/'); self.frames = {}; self.flags_override = flags
        for f in sorted(os.listdir(self.path)):
            m = FILE.match(f)
            if m: self.frames.setdefault(int(m[1]), {})[m[2]] = dict(name=f[:f.index('.bin') + 4], w=int(m[3]), h=int(m[4]), fmt=int(m[5]))
        self.ids = sorted(self.frames)
        self.create_flags = self._create_flags()
    def _create_flags(self):
        if self.flags_override is not None: return int(self.flags_override, 0), '--flags'
        for cand in (os.path.join(self.path, 'clip.txt'), os.path.join(os.path.dirname(self.path), 'create.txt')):
            if os.path.exists(cand):
                ls = [l for l in open(cand) if l.startswith('flags')]
                if ls:
                    if not cand.endswith('clip.txt'): print(f'  note: create flags taken from the LAST line of {cand} (older clips have no clip.txt): give --flags if another game ran since', file=sys.stderr)
                    return int(re.search(r'flags (0x[0-9a-f]+)', ls[-1])[1], 16), cand
        return None, None
    def params(self, k):
        t = open(os.path.join(self.path, f'frame_{k:03d}.txt')).read(); p = {}
        def g(key, n=1):
            m = re.search(rf'^{key} (\S+)' + (r' (\S+)' if n == 2 else ''), t, re.M); return None if not m else (float(m[1]) if n == 1 else [float(m[1]), float(m[2])])
        p['jitter'] = g('jitter', 2); p['motion_scale'] = g('motionVectorScale', 2); p['render_size'] = g('renderSize', 2); p['upscale_size'] = g('upscaleSize', 2)
        m = re.search(r'^enableSharpening (\S+) sharpness (\S+)', t, re.M); p['sharpening'] = int(m[1]) if m else None; p['sharpness'] = float(m[2]) if m else None
        p['frame_time_ms'] = g('frameTimeDelta'); p['pre_exposure'] = g('preExposure'); p['reset'] = int(g('reset') or 0)
        m = re.search(r'^cameraNear (\S+) cameraFar (\S+) fovV (\S+) viewSpaceToMeters (\S+)', t, re.M)
        if m: p['camera_near'], p['camera_far'], p['fov_vertical'], p['view_space_to_meters'] = map(float, m.groups())
        p['dispatch_flags'] = int(g('flags') or 0)
        m = re.search(r'^frame \d+ dispatch (\d+)', t, re.M); p['dispatch'] = int(m[1]) if m else None
        return p
    def read(self, k, kind):
        e = self.frames[k][kind]; return decode(load(os.path.join(self.path, e['name'])), e['w'], e['h'], e['fmt'])

def flags_info(fl):
    if fl is None: return {}
    return dict(create_flags=hex(fl), hdr=bool(fl & 1), motion_at_display_size=bool(fl & 2), motion_jittered=bool(fl & 4), depth_inverted=bool(fl & 8),
                depth_infinite=bool(fl & 16), auto_exposure=bool(fl & 32))

# ---------- previews ----------
def tonemap(c, pre=1.0):
    c = np.clip(np.nan_to_num(c[..., :3] / max(pre or 1.0, 1e-6), nan=0, posinf=1e4), 0, None)
    return (np.power(c / (1 + c), 1 / 2.2) * 255 + 0.5).astype(np.uint8)
def depth_gray(d):
    """brighter = larger stored value (whether that is near depends on the game: see depth_inverted)"""
    d = d.astype(np.float32); lo, hi = np.percentile(d, 1), np.percentile(d, 99)
    g = np.clip((d - lo) / max(hi - lo, 1e-12), 0, 1)
    return (g * 255 + 0.5).astype(np.uint8)
def motion_wheel(mv):
    x, y = mv[..., 0], mv[..., 1]; ang = (np.arctan2(y, x) / (2 * np.pi)) % 1; mag = np.hypot(x, y)
    s = np.clip(mag / max(np.percentile(mag, 99), 1e-6), 0, 1)
    h6 = ang * 6; i = np.floor(h6).astype(int) % 6; f = h6 - np.floor(h6); p = 1 - s; q = 1 - s * f; t = 1 - s * (1 - f); v = np.ones_like(s)
    r = np.choose(i, [v, q, p, p, t, v]); g = np.choose(i, [t, v, v, q, p, p]); b = np.choose(i, [p, p, t, v, v, q])
    return (np.stack([r, g, b], -1) * 255 + 0.5).astype(np.uint8)
def png(path, a):
    from PIL import Image; Image.fromarray(a).save(path, optimize=False, compress_level=3)

def exr(path, chans):
    """uncompressed scanline OpenEXR, 32-bit float channels: chans = {name: (H, W) float32}"""
    names = sorted(chans); h, w = chans[names[0]].shape
    def attr(n, t, v): return n.encode() + b'\0' + t.encode() + b'\0' + struct.pack('<i', len(v)) + v
    cl = b''.join(n.encode() + b'\0' + struct.pack('<iBBBBii', 2, 0, 0, 0, 0, 1, 1) for n in names) + b'\0'
    box = struct.pack('<iiii', 0, 0, w - 1, h - 1)
    hdr = (struct.pack('<ii', 20000630, 2) + attr('channels', 'chlist', cl) + attr('compression', 'compression', b'\0') + attr('dataWindow', 'box2i', box)
           + attr('displayWindow', 'box2i', box) + attr('lineOrder', 'lineOrder', b'\0') + attr('pixelAspectRatio', 'float', struct.pack('<f', 1))
           + attr('screenWindowCenter', 'v2f', struct.pack('<ff', 0, 0)) + attr('screenWindowWidth', 'float', struct.pack('<f', 1)) + b'\0')
    line = 8 + 4 * w * len(names); start = len(hdr) + 8 * h
    planes = [np.ascontiguousarray(chans[n], dtype='<f4') for n in names]
    with open(path, 'wb') as f:
        f.write(hdr); f.write(np.arange(h, dtype='<u8').__mul__(line).__add__(start).tobytes())
        for y in range(h): f.write(struct.pack('<ii', y, 4 * w * len(names))); [f.write(p[y].tobytes()) for p in planes]

# ---------- export ----------
def frange(spec, ids):
    if not spec: return ids
    a, b = spec.split(':'); a = int(a) if a else ids[0]; b = int(b) if b else ids[-1] + 1
    return [k for k in ids if a <= k < b]

def export(a):
    c = Clip(a.clip, a.flags); os.makedirs(a.out, exist_ok=True); ids = frange(a.frames, c.ids); fl = flags_info(c.create_flags[0])
    want = set(a.inputs.split(',')) if a.inputs else None; crop = [int(v) for v in a.crop.split(',')] if a.crop else None
    if not (a.npy or a.exr or a.png or a.mp4): a.npy = True
    rows = []; t0 = time.time()
    for n, k in enumerate(ids):
        p = c.params(k); p['frame'] = k; d = {}
        for kind, e in c.frames[k].items():
            if want and kind not in want: continue
            v = c.read(k, kind)
            if crop and v.ndim >= 2 and v.shape[0] > 1: x, y, w, h = crop; v = v[y:y + h, x:x + w]
            d[kind] = v; p[f'{kind}_format'] = FMT.get(e['fmt'], (0, f'DXGI {e["fmt"]}'))[1]; p[f'{kind}_size'] = [e['w'], e['h']]
            if kind == 'exposure': p['exposure'] = float(np.ravel(v)[0])
        if 'motion' in d and p.get('motion_scale'): d['motion_px'] = d['motion'][..., :2] * np.float32(p['motion_scale'])
        if a.npy:
            for kind, v in d.items():
                if kind != 'exposure': np.save(os.path.join(a.out, f'{kind}_{k:03d}.npy'), v)
        if a.exr:
            if 'color' in d: col = d['color']; exr(os.path.join(a.out, f'color_{k:03d}.exr'), {'R': col[..., 0], 'G': col[..., 1], 'B': col[..., 2]})
            if 'depth' in d: exr(os.path.join(a.out, f'depth_{k:03d}.exr'), {'Z': d['depth'].astype(np.float32)})
            if 'motion' in d: exr(os.path.join(a.out, f'motion_{k:03d}.exr'), {'R': d['motion'][..., 0], 'G': d['motion'][..., 1]})
        if a.png or a.mp4:
            if 'color' in d: png(os.path.join(a.out, f'color_{k:03d}.png'), tonemap(d['color'], p.get('pre_exposure') or 1.0))
            if a.png and 'depth' in d: png(os.path.join(a.out, f'depth_{k:03d}.png'), depth_gray(d['depth']))
            if a.png and 'motion' in d: png(os.path.join(a.out, f'motion_{k:03d}.png'), motion_wheel(d['motion_px'] if 'motion_px' in d else d['motion']))
        rows.append(p)
        if n % 20 == 0: print(f'  frame {k} ({n + 1}/{len(ids)}, {time.time() - t0:.0f} s)', file=sys.stderr)
    info = dict(clip=os.path.basename(c.path), frames=len(rows), create_flags_from=c.create_flags[1], **fl,
                values='color: linear HDR before FSR (pre-exposed by pre_exposure); depth as stored; motion as stored, motion_px = motion * motion_scale (render pixels)')
    json.dump(dict(clip=info, frames=rows), open(os.path.join(a.out, 'frames.json'), 'w'), indent=1)
    keys = [k for k in rows[0] if not isinstance(rows[0][k], (list, dict))]; lists = [k for k in rows[0] if isinstance(rows[0][k], list)]
    with open(os.path.join(a.out, 'frames.csv'), 'w', newline='') as f:
        wr = csv.writer(f); wr.writerow(keys + [f'{k}_{i}' for k in lists for i in 'xy'])
        for r in rows: wr.writerow([r.get(k) for k in keys] + [v for k in lists for v in (r.get(k) or [None, None])])
    if a.mp4:
        fps = 1000 / np.median([r['frame_time_ms'] for r in rows if r.get('frame_time_ms')] or [16.667])
        subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-framerate', f'{fps:.3f}', '-start_number', str(ids[0]), '-i', os.path.join(a.out, 'color_%03d.png'),
                        '-c:v', 'libx264', '-crf', '16', '-pix_fmt', 'yuv420p', '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2', os.path.join(a.out, 'color.mp4')], check=True)
        if not a.png: [os.remove(os.path.join(a.out, f'color_{k:03d}.png')) for k in ids]
    print(f'{c.path} -> {a.out}: {len(rows)} frames in {time.time() - t0:.0f} s')

def info(a):
    c = Clip(a.clip, a.flags); k0 = c.ids[0]; p = c.params(k0)
    print(f'{c.path}: frames {c.ids[0]}..{c.ids[-1]} ({len(c.ids)}), packed: {os.path.exists(os.path.join(c.path, "PACKED.txt"))}')
    for kind, e in c.frames[k0].items(): print(f'  {kind:9s} {e["w"]}x{e["h"]} {FMT.get(e["fmt"], (0, "DXGI " + str(e["fmt"])))[1]}')
    print(f'  render {p["render_size"]} -> {p["upscale_size"]}, frame time {p["frame_time_ms"]} ms, {flags_info(c.create_flags[0])} (from {c.create_flags[1]})')

# ---------- nr-record export (format nr-record-4, coding 3; written from Apex Compute's record/FORMAT.md) ----------
VK = {10: 97, 2: 109, 16: 103, 34: 83, 26: 122, 40: 126, 41: 100, 39: 100, 44: 126, 45: 126, 46: 126, 19: 126, 20: 126, 54: 76, 61: 9, 62: 13}
def nr_planes(v, fmt, kind):
    """decoded values -> (list of uint32 order-preserving planes, kinds byte per channel)"""
    def f16(a): b = np.asarray(a, np.float16).view(np.uint16).astype(np.uint32); return np.where(b & 0x8000, (~b) & 0xFFFF, b | 0x8000), 0x20
    def f32(a): b = np.asarray(a, np.float32).view(np.uint32).astype(np.uint64); return (np.where(b & 0x80000000, (~b) & 0xFFFFFFFF, b | 0x80000000)).astype(np.uint32), 0x40
    if fmt == 26: return None
    if fmt in (10, 34, 54): ch = [f16(v[..., i]) for i in range(v.shape[-1])] if v.ndim == 3 else [f16(v)]
    elif fmt in (2, 16): ch = [f32(v[..., i]) for i in range(v.shape[-1])]
    elif fmt in (39, 40, 41, 44, 45, 46, 19, 20): ch = [f32(v)]
    elif fmt in (61, 62): ch = [(v.astype(np.uint32), 8)]
    else: raise SystemExit(f'nr-record export: {kind} format DXGI {fmt} not supported')
    return [c[0] for c in ch], sum(c[1] << (8 * i) for i, c in enumerate(ch))

def zz(r, width):
    r = r.astype(np.int64) & ((1 << width) - 1); r = np.where(r >= 1 << (width - 1), r - (1 << width), r)        # nearest 0
    return ((r << 1) ^ (r >> 63)).astype(np.uint64) & ((1 << width) - 1)

def encode_texture(planes, widths, w, h):
    """coding 3 without flagged entries: per tile row the chunks' words. Returns list over tile rows of list over chunks of uint32 arrays."""
    tiles = (w + 7) // 8; chunks = (tiles + 31) // 32; W8 = tiles * 8; out = []
    res = []
    for pl, wd in zip(planes, widths):
        v = pl.astype(np.int64); r = np.empty_like(v)
        left = np.zeros_like(v); left[:, 1:] = v[:, :-1]
        up = np.zeros_like(v); up[1:] = v[:-1]; ul = np.zeros_like(v); ul[1:, 1:] = v[:-1, :-1]
        first = (np.arange(h) % 8 == 0)[:, None]
        r = np.where(first, v - left, v - left - up + ul)
        z = np.zeros((((h + 7) // 8) * 8, W8), np.uint64); z[:h, :w] = zz(r, wd); res.append(z)
    C = len(planes)
    for ty in range(len(res[0]) // 8):
        row = []
        for cx in range(chunks):
            t0 = cx * 32; n = min(32, tiles - t0)
            blocks = [res[c][ty * 8:ty * 8 + 8, (t0 + t) * 8:(t0 + t) * 8 + 8].reshape(64) for t in range(n) for c in range(C)]   # tile-major, channels inside
            bits = [int(b.max()).bit_length() for b in blocks]
            hdr = np.zeros((len(blocks) + 3) // 4 * 4, np.uint8); hdr[:len(bits)] = bits
            words = [hdr.view(np.uint32)]
            for b, nb in zip(blocks, bits):
                if nb == 0: continue
                bitarr = ((b[:, None] >> np.arange(nb, dtype=np.uint64)) & 1).astype(np.uint8).reshape(-1)     # field i: bits LSB first, fields in order
                words.append(np.packbits(bitarr, bitorder='little').view(np.uint32))
            row.append(np.concatenate(words))
        out.append(row)
    return out

def nrrecord(a):
    c = Clip(a.clip, a.flags); ids = frange(a.frames, c.ids); fl = c.create_flags[0] or 0; os.makedirs(a.out, exist_ok=True)
    roles = {'color': 'color', 'depth': 'depth', 'motion': 'motion', 'exposure': 'exposure', 'reactive': 'reactive', 'tc': 'transparency'}
    kinds0 = [k for k in ('color', 'depth', 'motion', 'exposure', 'reactive', 'tc') if k in c.frames[ids[0]]]
    p0 = c.params(ids[0]); ngx_flags = (1 if fl & 1 else 0) | (0 if fl & 2 else 2) | (4 if fl & 4 else 0) | (8 if fl & 8 else 0) | (64 if fl & 32 else 0)
    PARAMS = ['Width', 'Height', 'OutWidth', 'OutHeight', 'DLSS.Feature.Create.Flags', 'Reset', 'Sharpness', 'Jitter.Offset.X', 'Jitter.Offset.Y', 'MV.Scale.X', 'MV.Scale.Y',
              'DLSS.Pre.Exposure', 'DLSS.Render.Subrect.Dimensions.Width', 'DLSS.Render.Subrect.Dimensions.Height', 'FrameTimeDeltaInMsec', 'FSR.cameraNear',
              'FSR.cameraFar', 'FSR.cameraFovAngleVertical', 'FSR.frameTimeDelta', 'FSR.viewSpaceToMetersFactor', 'FSR.upscaleSize.width', 'FSR.upscaleSize.height']
    # textures, slot layout: word 0, then every texture's chunk table, then the chunks
    tex = []; at = 1
    for k in kinds0:
        e = c.frames[ids[0]][k]; w, h = e['w'], e['h']; tiles = (w + 7) // 8; chunks = (tiles + 31) // 32
        if e['fmt'] == 26: C, kinds = 3, 0x202020
        else:
            pl, kinds = nr_planes(c.read(ids[0], k), e['fmt'], k); C = len(pl)
        tex.append(dict(kind=k, role=roles[k], name=roles[k], api_format=e['fmt'], dxgi_format=e['fmt'], vk_format=VK.get(e['fmt'], 0), width=w, height=h, channels=C,
                        kinds=kinds, depth=e['fmt'] in (40, 44, 45, 46, 19, 20), srgb=False, reference=None, table=at))
        at += ((h + 7) // 8) * chunks * 2
    data_start = at; fb = open(os.path.join(a.out, 'frames.bin'), 'wb'); fi = open(os.path.join(a.out, 'frames.idx'), 'wb'); fp = open(os.path.join(a.out, 'params.bin'), 'wb')
    import zstandard; zc = zstandard.ZstdCompressor(level=3); BLK = 2097152; off = 0; maxw = 0; total_raw = 0; t0 = time.time(); qpc = 10000000; ts = 0
    for n, k in enumerate(ids):
        slot = [np.zeros(data_start, np.uint32)]; nxt = data_start
        for t in tex:
            e = c.frames[k][t['kind']]; raw = load(os.path.join(c.path, e['name']))
            if e['fmt'] == 26: planes = [pp.astype(np.uint32) for pp in np.moveaxis(r11g11b10_half(np.frombuffer(raw, np.uint32).reshape(e['h'], e['w'])), -1, 0)]; planes = [np.where(pp & 0x8000, (~pp) & 0xFFFF, pp | 0x8000).astype(np.uint32) for pp in planes]; widths = [16] * 3
            else:
                planes, _ = nr_planes(decode(raw, e['w'], e['h'], e['fmt']), e['fmt'], t['kind'])
                widths = [16 if (t['kinds'] >> (8 * i) & 0xE0) == 0x20 else 32 if (t['kinds'] >> (8 * i) & 0xE0) == 0x40 else (t['kinds'] >> (8 * i)) & 31 for i in range(len(planes))]
            enc = encode_texture(planes, widths, e['w'], e['h']); chunks = len(enc[0]); tbl = slot[0]
            for ty, row in enumerate(enc):
                for cx, words in enumerate(row):
                    i = t['table'] + 2 * (ty * chunks + cx); tbl[i] = nxt; tbl[i + 1] = len(words); slot.append(words); nxt += len(words)
        slot[0][0] = nxt; words = np.concatenate(slot).astype('<u4'); b = words.tobytes(); maxw = max(maxw, len(b)); total_raw += len(b)
        blocks = [b[i:i + BLK] for i in range(0, len(b), BLK)]; zs = [zc.compress(x) for x in blocks]
        body = b'NRZ1' + struct.pack('<IQIi', len(blocks), len(b), BLK, 3) + b''.join(struct.pack('<I', len(z)) for z in zs) + b''.join(zs)
        fb.write(body)
        p = c.params(k); ft = p.get('frame_time_ms') or 16.667; ts += int(ft * qpc / 1000)
        fi.write(struct.pack('<4sIQQQqIffIfI', b'NRI1', n, k, off, len(body), ts, 0, 0.0, 0.0, len(b), 0.0, 0)); off += len(body)
        rs, us = p.get('render_size') or [0, 0], p.get('upscale_size') or [0, 0]
        vals = {'Width': rs[0], 'Height': rs[1], 'OutWidth': us[0], 'OutHeight': us[1], 'DLSS.Feature.Create.Flags': ngx_flags, 'Reset': p.get('reset', 0),
                'Sharpness': p.get('sharpness'), 'Jitter.Offset.X': (p.get('jitter') or [0, 0])[0], 'Jitter.Offset.Y': (p.get('jitter') or [0, 0])[1],
                'MV.Scale.X': (p.get('motion_scale') or [1, 1])[0], 'MV.Scale.Y': (p.get('motion_scale') or [1, 1])[1], 'DLSS.Pre.Exposure': p.get('pre_exposure'),
                'DLSS.Render.Subrect.Dimensions.Width': rs[0], 'DLSS.Render.Subrect.Dimensions.Height': rs[1], 'FrameTimeDeltaInMsec': ft, 'FSR.cameraNear': p.get('camera_near'),
                'FSR.cameraFar': p.get('camera_far'), 'FSR.cameraFovAngleVertical': p.get('fov_vertical'), 'FSR.frameTimeDelta': ft,
                'FSR.viewSpaceToMetersFactor': p.get('view_space_to_meters'), 'FSR.upscaleSize.width': us[0], 'FSR.upscaleSize.height': us[1]}
        rec = struct.pack('<4sIIIQq', b'NRP1', 0, 1, 0, k, ts)
        for name in PARAMS:
            v = vals.get(name)
            if v is None: rec += struct.pack('<IIf', 0, 0, 0.0)
            elif float(v).is_integer() and v >= 0: rec += struct.pack('<IIf', 3, int(v), float(v))
            else: rec += struct.pack('<IIf', 2, 0, float(v))
        fp.write(rec)
        if n % 20 == 0: print(f'  frame {k} ({n + 1}/{len(ids)}, {time.time() - t0:.0f} s)', file=sys.stderr)
    fb.close(); fi.close(); fp.close()
    create = {n: {'u': int(vals[n]), 'f': float(vals[n])} for n in ('Width', 'Height', 'OutWidth', 'OutHeight', 'DLSS.Feature.Create.Flags')}
    j = dict(format='nr-record-4', coding=3, feature='sr', source='ffx-upscale', graphics_api='d3d12', recorder='cybocap.py nr-record export (from a cybocap clip)',
             game='', time=time.strftime('%Y-%m-%dT%H:%M:%S'), width=int(p0['upscale_size'][0]), height=int(p0['upscale_size'][1]), create=create, forward=None,
             hdr=bool(fl & 1), qpc_frequency=qpc, data_start=data_start, slot_bytes=maxw, ring=1,
             textures=[{k: v for k, v in t.items() if k != 'kind'} for t in tex], parameter_source='ngx', parameters=PARAMS, matrices=[],
             call_bytes=32 + 12 * len(PARAMS), index_bytes=64, zstd_level=3, block_bytes=BLK, status='complete', frames=len(ids), dropped=0, lost=0,
             bytes=off, raw_bytes=total_raw, stored_frames=0)
    json.dump(j, open(os.path.join(a.out, 'record.json'), 'w'), indent=1)
    print(f'{c.path} -> {a.out}: nr-record-4, {len(ids)} frames, {off / 1e9:.2f} GB in {time.time() - t0:.0f} s')

# ---------- share: one file to upload + the message with its link ----------
REPO = 'zgauthier2000/cybocap'
TERMS = """Sharing a capture with the CyboCap project
  - You upload the file yourself, to storage you choose (Google Drive, MEGA, Dropbox, your own server, ...), and you send us the link.
    You are responsible for that upload and for the link you share.
  - Only share captures you are allowed to share (check the game's terms).
  - We use shared captures to test and train upscalers. We do not republish your raw capture; pictures made from it may appear in
    comparisons. The file holds the game's images and settings only: the game's executable name, no user names or paths."""

_H = (
    '8bad2094be156a2188e856809c0110e094e7ec8321191efd'
    'bdfcb5457aeb2da6475e911ceb6960a6253c885e5fbbb575'
    'feb3d83ec48fd603692108315e99a736c3c844cf33b3a199'
    '4b5455998a1f2ae91b76723fac8e08c7a8fae889e70be445'
    'b101fa559d4e1466beeb61b3b7efcb13a98e8494476b29e2'
    '8f')      # where capture links are sent, encoded (set by the maintainers; empty = no sending)

def _hook():
    import hashlib
    if not _H: return None
    k = hashlib.sha256(b'cybocap share').digest(); b = bytes.fromhex(''.join(_H))[::-1]
    return bytes(c ^ k[i % 32] ^ (i * 11 & 255) for i, c in enumerate(b)).decode()

def send_discord(msg):
    import urllib.request
    h = _hook()
    if not h: return False, 'no address to send to in this copy'
    body = json.dumps({'content': msg[:1990], 'username': 'cybocap', 'allowed_mentions': {'parse': []}}).encode()
    req = urllib.request.Request(h, data=body, headers={'Content-Type': 'application/json', 'User-Agent': 'cybocap'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r: return 200 <= r.status < 300, f'HTTP {r.status}'
    except Exception as e: return False, str(e)

def share(a):
    import tarfile, hashlib, webbrowser, urllib.parse, shutil
    clip = a.clip.rstrip('/\\')
    print(TERMS)
    if not a.agree:
        if not sys.stdin.isatty(): sys.exit('add --agree to confirm the terms above')
        if input('\nType yes to go on: ').strip().lower() != 'yes': sys.exit('stopped')
    c = Clip(clip, a.flags)
    if os.path.exists(os.path.join(clip, 'PACKED.txt')): packed, made = clip, False
    else:
        from clippack import pack
        packed = clip + '.packed'
        if not os.path.exists(os.path.join(packed, 'PACKED.txt')):
            print('packing (lossless, the original is kept) ...')
            if not pack(clip, 3, a.jobs, None, False): sys.exit('packing failed: nothing to share')
        made = True
    game = 'unknown'
    ct = os.path.join(packed, 'clip.txt')
    if os.path.exists(ct):
        m = re.search(r'^game (.+)$', open(ct).read(), re.M); game = m[1].strip() if m else game
    base = os.path.basename(clip)
    arc = os.path.join(a.out or os.path.dirname(os.path.abspath(clip)), f'{base}_{re.sub(r"[^A-Za-z0-9]+", "-", os.path.splitext(game)[0]).strip("-") or "game"}.cybocap.tar')
    if not os.path.exists(arc):
        print(f'writing {arc} ...')
        with tarfile.open(arc + '.part', 'w', format=tarfile.PAX_FORMAT) as t:
            for f in sorted(os.listdir(packed)):
                ti = t.gettarinfo(os.path.join(packed, f), arcname=f'{base}/{f}'); ti.mtime = 0; ti.uid = ti.gid = 0; ti.uname = ti.gname = ''
                with open(os.path.join(packed, f), 'rb') as fh: t.addfile(ti, fh)
        os.replace(arc + '.part', arc)
    h = hashlib.sha256()
    with open(arc, 'rb') as fh:
        for b in iter(lambda: fh.read(1 << 24), b''): h.update(b)
    sha = h.hexdigest(); size = os.path.getsize(arc)
    p0 = c.params(c.ids[0]); fl = flags_info(c.create_flags[0])
    summary = (f'game: {game}\nclip: {base}\nframes: {len(c.ids)}\nrender: {int(p0["render_size"][0])}x{int(p0["render_size"][1])} -> '
               f'{int(p0["upscale_size"][0])}x{int(p0["upscale_size"][1])}\nframe time: {p0["frame_time_ms"]:.2f} ms\nflags: {fl.get("create_flags")}\n'
               f'file: {os.path.basename(arc)}\nsize: {size} bytes ({size / 1e9:.2f} GB)\nsha256: {sha}\n')
    open(arc + '.txt', 'w').write(summary)
    if made and not a.keep_packed: shutil.rmtree(packed)      # the .packed copy made only for sharing; the original clip stays
    print(f'\n{summary}\nUpload {arc} to storage of your choice and make the link downloadable without signing in.')
    link = a.link
    if not link and sys.stdin.isatty(): link = input('Paste the download link here (or press Enter to do it later with --link): ').strip()
    if not link: print(f'Later: python cybocap.py share "{clip}" --agree --link <url>'); return
    if not re.match(r'^https?://\S+$', link): sys.exit('that does not look like a link (https://...)')
    msg = f'CyboCap capture: {game}\n\nlink: {link}\n' + summary + f'tool: cybocap.py share\n'
    open(arc + '.txt', 'w').write(msg)
    url = f'https://github.com/{REPO}/issues/new?' + urllib.parse.urlencode({'title': f'Capture: {game} ({len(c.ids)} frames)', 'labels': 'capture', 'body': msg})
    go = a.send
    if not go and _H and sys.stdin.isatty(): go = input('\nSend the link and these details to Cyboman now? (yes / no): ').strip().lower() == 'yes'
    if go:
        ok, why = send_discord(msg); print('sent, thank you' if ok else f'NOT sent ({why}): use the message below')
        if ok: return
    print(f'\n---- or send this yourself (for example in the CyboCap Discord), or open the GitHub link below ----\n{msg}----\nGitHub (opens a pre-filled issue; you post it under your own account):\n{url}')
    if a.open: webbrowser.open(url)

if __name__ == '__main__':
    ap = argparse.ArgumentParser(); ap.add_argument('cmd', choices=['info', 'export', 'nrrecord', 'share']); ap.add_argument('clip'); ap.add_argument('out', nargs='?')
    for f in ('--npy', '--exr', '--png', '--mp4'): ap.add_argument(f, action='store_true')
    ap.add_argument('--frames'); ap.add_argument('--flags', help='the FSR create flags, e.g. 0x9, for clips without clip.txt'); ap.add_argument('--inputs'); ap.add_argument('--crop')
    ap.add_argument('--link'); ap.add_argument('--agree', action='store_true'); ap.add_argument('--open', action='store_true'); ap.add_argument('--send', action='store_true'); ap.add_argument('--keep-packed', action='store_true'); ap.add_argument('--jobs', type=int, default=4)
    a = ap.parse_args()
    {'info': info, 'export': export, 'nrrecord': nrrecord, 'share': share}[a.cmd](a)
