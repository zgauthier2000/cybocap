#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# clippack.py: OPTIONAL lossless packing of cybocap clips (about half the size). Each frame_*.bin becomes frame_*.bin.s<k>.zst: the bytes regrouped
# by position within a k-byte texel (k = file size / (width * height) from the name), then zstd. cybocap.py reads packed and plain clips alike.
#
#   clippack.py pack <clip>... [--level 3] [--jobs 4] [--replace]
#       writes <clip>.packed/, decodes EVERY packed file and compares it with the original, writes PACKED.txt (sha256 of every original file).
#       The original clip is kept unless --replace is given: then it is deleted after the check and the packed folder takes its name.
#   clippack.py unpack <packed clip> <out dir>   restores the original files (checked against PACKED.txt)
#   clippack.py verify <packed clip>...           decodes every file and compares with PACKED.txt
#   from clippack import load; load(path)         bytes of a clip file, packed or not (give the .bin name)
# Needs: numpy, zstandard.
import sys, os, re, glob, hashlib, shutil, argparse, time
from multiprocessing import Pool
import numpy as np, zstandard

NAME = re.compile(r'_(\d+)x(\d+)_f\d+\.bin$')

def stride(name, size):
    m = NAME.search(name)
    if m:
        px = int(m.group(1)) * int(m.group(2))
        if px and size % px == 0 and 1 <= size // px <= 16: return size // px
    return 1

def shuffle(b, k):
    if k == 1 or len(b) % k: return b
    return np.frombuffer(b, np.uint8).reshape(-1, k).T.tobytes()

def unshuffle(b, k):
    if k == 1 or len(b) % k: return b
    return np.frombuffer(b, np.uint8).reshape(k, -1).T.tobytes()

def decode(path):
    k = int(re.search(r'\.s(\d+)\.zst$', path).group(1))
    return unshuffle(zstandard.ZstdDecompressor().decompress(open(path, 'rb').read()), k)

# ---------- GPU-packed files (*.bin.gp, written by the DLL with GpuPack=1; layout in docs/FORMAT.md) ----------
GP_CH = {0: [(0, 11), (11, 11), (22, 10)], 1: [(0, 24), (24, 8)], 2: [(0, 32), (0, 32)], 3: [(0, 32)], 4: [(0, 16), (16, 16), (0, 16), (16, 16)], 5: [(0, 16), (16, 16)]}

def _bits(S64, pos, width):
    """width (0..32) bits starting at bit `pos` of the word array (vectorized)"""
    w = (pos >> 5).astype(np.int64); sh = (pos & 31).astype(np.uint64)
    v = (S64[w] | (S64[w + 1] << np.uint64(32))) >> sh
    return v & ((np.uint64(1) << width.astype(np.uint64)) - np.uint64(1))

def gp_decode(data):
    """bytes of a .bin.gp file -> the bytes the raw .bin would have held"""
    S = np.frombuffer(data, '<u4'); used, magic, ver, W, H, F, TX, TY = (int(v) for v in S[:8])
    if magic != 0x50475943 or ver != 1 or used != len(S): raise ValueError('not a CyboCap GPU-packed file (or truncated)')
    S64 = np.concatenate([S, np.zeros(2, np.uint32)]).astype(np.uint64); nt = TX * TY; ch = GP_CH[F]; C = len(ch)
    rec = S[8:8 + nt].astype(np.int64); h0 = S64[rec]; h1 = S64[rec + 1]; at = rec + 2
    i64 = np.arange(64, dtype=np.int64)
    out = []
    for c in range(C):
        w = ch[c][1]; b = ((h0 >> np.uint64(8 * c)) & np.uint64(127)).astype(np.int64); f = ((h0 >> np.uint64(8 * c + 7)) & np.uint64(1)).astype(np.int64)
        E = ((h1 >> np.uint64(8 * c)) & np.uint64(255)).astype(np.int64); fw = b + f
        pos = at[:, None] * 32 + i64[None, :] * fw[:, None]
        val = _bits(S64, pos, np.broadcast_to(fw[:, None], pos.shape))
        low = val & ((np.uint64(1) << b[:, None].astype(np.uint64)) - np.uint64(1))
        flag = ((val >> b[:, None].astype(np.uint64)) & np.uint64(1)).astype(bool) & (f[:, None] == 1)
        nflag = flag.sum(1); ext = at + 2 * fw
        rank = np.cumsum(flag, 1) - 1
        hpos = ext[:, None] * 32 + rank * E[:, None]
        hi = np.where(flag, _bits(S64, np.where(flag, hpos, 0), np.broadcast_to(E[:, None], hpos.shape)), np.uint64(0))
        z = (low | (hi << b[:, None].astype(np.uint64))).astype(np.int64)
        at = ext + (nflag * E + 31) // 32
        r = (z >> 1) ^ -(z & 1)                                       # unzigzag
        img = r.reshape(TY, TX, 8, 8).transpose(0, 2, 1, 3).reshape(TY * 8, TX * 8)
        img = np.cumsum(img, axis=1).reshape(TY, 8, TX * 8).cumsum(axis=1).reshape(TY * 8, TX * 8)[:H, :W]
        out.append(img.astype(np.uint64) & np.uint64((1 << w) - 1))
    def unflip32(v): return np.where(v & np.uint64(0x80000000), v ^ np.uint64(0x80000000), (~v) & np.uint64(0xFFFFFFFF))
    def unflip16(v): return np.where(v & np.uint64(0x8000), v ^ np.uint64(0x8000), (~v) & np.uint64(0xFFFF))
    if F == 0: words = out[0] | (out[1] << np.uint64(11)) | (out[2] << np.uint64(22)); return words.astype('<u4').tobytes()
    if F == 1: return (out[0] | (out[1] << np.uint64(24))).astype('<u4').tobytes()
    if F == 2: return np.stack([unflip32(out[0]), unflip32(out[1])], -1).astype('<u4').tobytes()
    if F == 3: return unflip32(out[0]).astype('<u4').tobytes()
    if F == 4: return np.stack([unflip16(o) for o in out], -1).astype('<u2').tobytes()
    if F == 5: return np.stack([unflip16(out[0]), unflip16(out[1])], -1).astype('<u2').tobytes()

def clipglob(pattern):
    """glob for clip files that also finds packed ones: a pattern ending in .bin matches x.bin, x.bin.gp and x.bin.s<k>.zst;
    returns the .bin names (give them to load() / fromfile())"""
    names = set(glob.glob(pattern)) | {p[:-3] for p in glob.glob(pattern + '.gp')} | {p[:p.rindex('.bin') + 4] for p in glob.glob(pattern + '.s*.zst')}
    return sorted(names)

def fromfile(path, dtype):
    """np.fromfile for clip files, packed or not"""
    return np.frombuffer(load(path), dtype).copy()

def load(path):
    """Bytes of a clip file: the .bin itself, its GPU-packed twin <path>.gp, or its packed twin <path>.s<k>.zst."""
    if os.path.exists(path): return open(path, 'rb').read()
    if os.path.exists(path + '.gp'): return gp_decode(open(path + '.gp', 'rb').read())
    z = glob.glob(glob.escape(path) + '.s*.zst')
    if not z: raise FileNotFoundError(path)
    return decode(z[0])

def pack_one(a):
    src, dst_dir, level = a
    raw = open(src, 'rb').read(); name = os.path.basename(src); k = stride(name, len(raw))
    z = zstandard.ZstdCompressor(level=level, write_content_size=True).compress(shuffle(raw, k))
    out = os.path.join(dst_dir, f'{name}.s{k}.zst')
    with open(out, 'wb') as f: f.write(z)
    ok = decode(out) == raw                                   # check from the file just written
    return name, len(raw), len(z), hashlib.sha256(raw).hexdigest(), ok

def pack(clip, level, jobs, prog=None, replace=False):
    clip = clip.rstrip('/')
    if os.path.exists(os.path.join(clip, 'PACKED.txt')): print(f'{clip}: already packed, skipped'); return True
    tmp = clip + ('.packing' if replace else '.packed')
    if os.path.exists(tmp): print(f'{clip}: {tmp} exists (an interrupted run?): remove it first'); return False
    os.makedirs(tmp)
    bins = sorted(glob.glob(os.path.join(glob.escape(clip), '*.bin'))); others = [f for f in sorted(os.listdir(clip)) if not f.endswith('.bin')]
    for o in others:
        p = os.path.join(clip, o)
        if os.path.isdir(p): print(f'{clip}: unexpected folder {o}: stopped'); return False
        shutil.copy2(p, tmp)
    t0 = time.time(); rows = []; bad = 0
    with Pool(jobs) as pool:
        for r in pool.imap_unordered(pack_one, [(b, tmp, level) for b in bins], chunksize=2):
            rows.append(r); bad += not r[4]
            if prog: prog.tick(os.path.basename(clip))
    raw = sum(r[1] for r in rows); packed = sum(r[2] for r in rows)
    if bad or len(rows) != len(bins):
        print(f'{clip}: CHECK FAILED ({bad} files did not decode to the original, {len(rows)}/{len(bins)} packed): original kept, {tmp} left for inspection'); return False
    for o in others:                                                # copied side files must match too
        if open(os.path.join(clip, o), 'rb').read() != open(os.path.join(tmp, o), 'rb').read(): print(f'{clip}: side file {o} differs: stopped'); return False
    with open(os.path.join(tmp, 'PACKED.txt'), 'w') as f:
        f.write(f'# packed by clippack.py (shuffle + zstd level {level}) on {time.strftime("%Y-%m-%d %I:%M %p")}: {len(rows)} files, {raw} -> {packed} bytes '
                f'({100 * packed / max(raw, 1):.1f}%); every file decoded back to the original bytes before the original was removed\n# name\tbytes\tsha256\n')
        for r in sorted(rows): f.write(f'{r[0]}\t{r[1]}\t{r[3]}\n')
    if not replace:
        print(f'{clip}: {len(rows)} files {raw / 1e9:.2f} -> {packed / 1e9:.2f} GB ({100 * packed / raw:.1f}%) in {time.time() - t0:.0f} s, all checked; packed copy {tmp}, original kept')
        return True
    cmd = os.environ.get('CLIPPACK_DELETE')                       # optional: a command that deletes the original folder instead (e.g. a safe-delete script)
    if cmd: import subprocess; rc = subprocess.run(cmd.split() + [clip], capture_output=True, text=True); msg = (rc.stdout.strip().splitlines() or ['?'])[-1]
    else: shutil.rmtree(clip); msg = 'removed'
    if os.path.exists(clip):
        print(f'{clip}: packed and checked, but the original could not be removed ({msg}): both kept'); return False
    os.rename(tmp, clip)
    print(f'{clip}: {len(rows)} files {raw / 1e9:.2f} -> {packed / 1e9:.2f} GB ({100 * packed / raw:.1f}%) in {time.time() - t0:.0f} s, all checked')
    return True

def table(clip):
    rows = [l.rstrip('\n').split('\t') for l in open(os.path.join(clip, 'PACKED.txt')) if not l.startswith('#')]
    return {r[0]: (int(r[1]), r[2]) for r in rows}

def verify(clip):
    t = table(clip); bad = 0
    for name, (n, h) in t.items():
        b = load(os.path.join(clip, name)); bad += len(b) != n or hashlib.sha256(b).hexdigest() != h
    print(f'{clip}: {len(t)} files, {bad} differ from PACKED.txt'); return bad == 0

def unpack(clip, out):
    os.makedirs(out, exist_ok=True); t = table(clip)
    for f in os.listdir(clip):
        if not f.endswith('.zst') and f != 'PACKED.txt': shutil.copy2(os.path.join(clip, f), out)
    for name, (n, h) in t.items():
        b = load(os.path.join(clip, name))
        if hashlib.sha256(b).hexdigest() != h: sys.exit(f'{name}: does not match PACKED.txt')
        open(os.path.join(out, name), 'wb').write(b)
    print(f'{clip} -> {out}: {len(t)} files restored and checked')

if __name__ == '__main__':
    ap = argparse.ArgumentParser(); ap.add_argument('cmd', choices=['pack', 'unpack', 'verify']); ap.add_argument('paths', nargs='+')
    ap.add_argument('--level', type=int, default=3); ap.add_argument('--jobs', type=int, default=4); ap.add_argument('--replace', action='store_true'); a = ap.parse_args()
    if a.cmd == 'pack':
        class Prog:
            def __init__(self, title, total): self.n, self.total = 0, total
            def tick(self, label): self.n += 1; print(f'\r  {self.n}/{self.total} {label}   ', end='', file=sys.stderr, flush=True)
            def done(self): print(file=sys.stderr)
        n = sum(len(glob.glob(os.path.join(glob.escape(c.rstrip('/')), '*.bin'))) for c in a.paths)
        p = Prog(f'Packing {len(a.paths)} capture clips', total=max(n, 1)); ok = all([pack(c, a.level, a.jobs, p, a.replace) for c in a.paths]); p.done()
        sys.exit(0 if ok else 1)
    if a.cmd == 'verify': sys.exit(0 if all([verify(c) for c in a.paths]) else 1)
    unpack(a.paths[0], a.paths[1])
