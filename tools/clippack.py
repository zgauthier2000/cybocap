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

def load(path):
    """Bytes of a clip file: the .bin itself, or its packed twin <path>.s<k>.zst."""
    if os.path.exists(path): return open(path, 'rb').read()
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
    shutil.rmtree(clip); msg = 'removed'
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
