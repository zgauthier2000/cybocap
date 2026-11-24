// gpupack.hlsl: CyboCap's lossless GPU packing of one captured input (compiled to DXIL, embedded in the DLL). One 64-thread group per 8x8 tile,
// thread = texel. Residual: v - left - up + up-left inside each 8-row tile row (first row of a tile row: v - left), modulo 2^width, nearest-zero,
// zigzag. Per tile and channel the cheapest of: every field B bits (B = bits of the largest residual), or b+1 bits per field (b low bits + a flag)
// with each flagged residual's high B-b bits after the fields, in texel order. No wave-size assumption (groupshared atomics only).
//
// Output buffer (32-bit words): [0] allocator (starts at the first data word), [1..7] header (see docs/FORMAT.md), [8 ...] one index word per tile
// (the tile record's first word), then tile records in any order: word 0 = per channel b | 0x80 if flagged (8 bits each), word 1 = per channel
// B-b, then per channel 2*(b+f) field words and ceil(flagged*(B-b)/32) extra words.
// Formats (FMT): 0 R11G11B10 (11/11/10 bits as stored), 1 R24G8 / D24S8 (24 + 8), 2 RG32F, 3 R32F / D32F, 4 RGBA16F, 5 RG16F
// (floats as order-preserving integers).
cbuffer C : register(b0) { uint W, H, FMT, pitch, tilesX, index; };   // pitch: input row pitch in bytes
ByteAddressBuffer In : register(t0);
RWByteAddressBuffer Out : register(u0);
groupshared uint hist[4][33];
groupshared uint bsel[4], fsel[4], esel[4], nflag[4];
groupshared uint fmask[4][2];
groupshared uint packed[4][130];
groupshared uint recWords;
groupshared uint base;
uint NC() { return FMT == 0 ? 3u : FMT == 4 ? 4u : (FMT == 1 || FMT == 2 || FMT == 5) ? 2u : 1u; }
uint WD(uint c) { return FMT == 0 ? (c == 2 ? 10u : 11u) : FMT == 1 ? (c == 0 ? 24u : 8u) : (FMT == 4 || FMT == 5) ? 16u : 32u; }
uint F32(uint b) { return (b & 0x80000000u) != 0 ? ~b : (b | 0x80000000u); }
uint F16(uint b) { return ((b & 0x8000u) != 0 ? ~b : (b | 0x8000u)) & 0xFFFFu; }
uint LM(uint n) { return n >= 32u ? 0xFFFFFFFFu : (1u << n) - 1u; }
uint NB(uint v) { return v == 0u ? 0u : firstbithigh(v) + 1u; }
uint Val(uint x, uint y, uint c) {                                   // channel c of texel (x, y) as an integer of WD(c) bits
    uint row = y * pitch;
    if (FMT == 0) { uint w = In.Load(row + 4 * x); return c == 0 ? (w & 0x7FFu) : c == 1 ? ((w >> 11) & 0x7FFu) : (w >> 22); }
    if (FMT == 1) { uint w = In.Load(row + 4 * x); return c == 0 ? (w & 0xFFFFFFu) : (w >> 24); }
    if (FMT == 2) return F32(In.Load(row + 8 * x + 4 * c));
    if (FMT == 3) return F32(In.Load(row + 4 * x));
    if (FMT == 4) { uint w = In.Load(row + 8 * x + 4 * (c >> 1)); return F16((c & 1u) != 0 ? (w >> 16) : w); }
    uint w = In.Load(row + 4 * x); return F16((c & 1u) != 0 ? (w >> 16) : w);
}
[numthreads(64, 1, 1)]
void main(uint3 gid : SV_GroupID, uint lane : SV_GroupIndex) {
    uint tile = gid.y * 65535u + gid.x, tilesY = (H + 7) / 8, nc = NC();
    uint tx = tile % tilesX, ty = tile / tilesX, x = tx * 8 + lane % 8, y = ty * 8 + lane / 8;
    bool in_ = tile < tilesX * tilesY && x < W && y < H, top = (y % 8) == 0;
    for (uint i = lane; i < 4 * 33; i += 64) hist[i / 33][i % 33] = 0u;
    for (uint j = lane; j < 4 * 130; j += 64) packed[j / 130][j % 130] = 0u;
    if (lane < 8) fmask[lane / 2][lane % 2] = 0u;
    GroupMemoryBarrierWithGroupSync();
    uint z[4];
    [unroll] for (uint c = 0; c < 4; c++) {
        z[c] = 0u;
        if (c < nc && in_) {
            uint w = WD(c), m = LM(w), v = Val(x, y, c), l = x > 0 ? Val(x - 1, y, c) : 0u, r;
            if (top) r = v - l; else r = v - l - Val(x, y - 1, c) + (x > 0 ? Val(x - 1, y - 1, c) : 0u);
            r &= m; int s = (w == 32) ? int(r) : (r >= (1u << (w - 1)) ? int(r) - int(1u << w) : int(r));
            z[c] = ((uint(s) << 1) ^ uint(s >> 31)) & m;
        }
        if (c < nc) InterlockedAdd(hist[c][NB(z[c])], 1u);
    }
    GroupMemoryBarrierWithGroupSync();
    if (lane < nc) {
        uint c = lane, B = 0, k;
        for (k = 32; k > 0; k--) if (hist[c][k] != 0u) { B = k; break; }
        uint best = 64u * B, bb = B, ff = 0, gt = 0, gtb = 0;
        for (int b = int(B) - 1; b >= 0; b--) {
            gt += hist[c][b + 1];
            uint cost = 64u * (uint(b) + 1u) + gt * (B - uint(b)) + 8u;
            if (cost < best) { best = cost; bb = uint(b); ff = 1; gtb = gt; }
        }
        bsel[c] = bb; fsel[c] = ff; esel[c] = ff == 1u ? B - bb : 0u; nflag[c] = ff == 1u ? gtb : 0u;
    }
    GroupMemoryBarrierWithGroupSync();
    [unroll] for (uint c2 = 0; c2 < 4; c2++)
        if (c2 < nc && fsel[c2] == 1u && NB(z[c2]) > bsel[c2]) { uint o; InterlockedOr(fmask[c2][lane >> 5], 1u << (lane & 31u), o); }
    GroupMemoryBarrierWithGroupSync();
    uint fw[4], ew[4], total = 2u;
    [unroll] for (uint c3 = 0; c3 < 4; c3++) {
        fw[c3] = c3 < nc ? 2u * (bsel[c3] + fsel[c3]) : 0u; ew[c3] = c3 < nc ? (nflag[c3] * esel[c3] + 31u) / 32u : 0u; total += fw[c3] + ew[c3];
    }
    [unroll] for (uint c4 = 0; c4 < 4; c4++) {
        if (c4 >= nc || !in_) continue;
        uint b = bsel[c4], f = fsel[c4], fwid = b + f, o;
        bool flagged = f == 1u && NB(z[c4]) > b;
        if (fwid > 0u) {
            uint val = (z[c4] & LM(b)) | ((flagged ? 1u : 0u) << b), pos = lane * fwid, wi = pos >> 5, sh = pos & 31u;
            InterlockedOr(packed[c4][wi], val << sh, o);
            if (sh + fwid > 32u) InterlockedOr(packed[c4][wi + 1], val >> (32u - sh), o);
        }
        if (flagged) {
            uint E = esel[c4], hi = z[c4] >> b, m0 = fmask[c4][0], m1 = fmask[c4][1];
            uint rank = lane < 32u ? countbits(m0 & LM(lane)) : countbits(m0) + countbits(m1 & LM(lane - 32u));
            uint pos = rank * E, wi = fw[c4] + (pos >> 5), sh = pos & 31u;
            InterlockedOr(packed[c4][wi], hi << sh, o);
            if (sh + E > 32u) InterlockedOr(packed[c4][wi + 1], hi >> (32u - sh), o);
        }
    }
    GroupMemoryBarrierWithGroupSync();
    if (tile >= tilesX * tilesY) return;
    if (lane == 0) { uint b0; Out.InterlockedAdd(0, total, b0); base = b0; }
    GroupMemoryBarrierWithGroupSync();
    if (lane == 0) {
        Out.Store(4 * (index + tile), base);
        uint h0 = 0, h1 = 0;
        for (uint c5 = 0; c5 < nc; c5++) { h0 |= (bsel[c5] | (fsel[c5] << 7)) << (8 * c5); h1 |= esel[c5] << (8 * c5); }
        Out.Store(4 * base, h0); Out.Store(4 * (base + 1), h1);
    }
    uint o2 = base + 2u;
    [unroll] for (uint c6 = 0; c6 < 4; c6++) {
        if (c6 >= nc) continue;
        uint nw = fw[c6] + ew[c6];
        for (uint i2 = lane; i2 < nw; i2 += 64) Out.Store(4 * (o2 + i2), packed[c6][i2]);
        o2 += nw;
    }
}
