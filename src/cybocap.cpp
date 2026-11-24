// SPDX-License-Identifier: GPL-2.0-or-later
// CyboCap 1.2, FSR capture: an input capture proxy for OptiScaler's FSR upscaler slot (D3D12).
//
// Stands in for amd_fidelityfx_upscaler_dx12.dll, forwards every call to the real DLL (renamed amd_fidelityfx_upscaler_dx12_real.dll next to it)
// and, while recording, copies each upscale dispatch's inputs (color, depth, motion vectors, exposure, reactive, transparency & composition) into
// readback buffers on the game's own command list, before FSR runs. A writer thread saves them to disk.
//
// Hotkeys: F10 records a clip of `Seconds` (default 2), Shift+F10 the longest clip that does not make the game wait for the disk; either key
// during a clip ends it early
// (ends when the readback ring is about to run out; capped at `LongSeconds`). Settings: cybocap.ini next to the DLL (see cybocap.ini.example);
// environment variables CYBOCAP_DIR, CYBOCAP_FRAMES, CYBOCAP_LONG_FRAMES, CYBOCAP_WRITERS, CYBOCAP_SLOTS override it.
// Output: <Folder>\clip_<time>\frame_NNN_<input>_<w>x<h>_f<DXGI format>.bin (rows packed), frame_NNN.txt (dispatch parameters), clip.txt.
// FastUpscaler=1 (default): the upscale context runs the fastest FSR version in the real DLL (FSR 3.1.x), whatever OptiScaler asked for.
// GpuPack=1 (default): color, depth and motion are packed losslessly on the GPU (shaders/gpupack.hlsl) and written as *.bin.gp (docs/FORMAT.md).
// See docs/FORMAT.md. Test hooks: CYBOCAP_AUTO=<n> starts a clip after n dispatches (CYBOCAP_AUTO_LONG=1: a long one), CYBOCAP_PROFILE=1 logs timings.
#define WIDL_EXPLICIT_AGGREGATE_RETURNS
#include <windows.h>
#include <d3d12.h>
#include <cstdio>
#include <cstdint>
#include <cstring>
#include "ffx_api.h"
#include "ffx_upscale.h"
#include "dx12/ffx_api_dx12.h"
#include "shaders/gpupack_dxil.h"      // g_gpupack: shaders/gpupack.hlsl compiled by dxc (signed DXIL)
#ifndef REAL_DLL
#define REAL_DLL "amd_fidelityfx_upscaler_dx12_real.dll"
#endif
#include <cstdarg>
#include <thread>
#include <mutex>
#include <condition_variable>
#include <deque>
#include <vector>
#include <string>
#include <algorithm>

static HMODULE self, real;
static PfnFfxCreateContext pCreate; static PfnFfxDestroyContext pDestroy; static PfnFfxConfigure pConfigure; static PfnFfxQuery pQuery; static PfnFfxDispatch pDispatch;
static char base[MAX_PATH], clip[MAX_PATH]; static FILE* logf;

static double shortSec = 2, longSec = 20; static bool longStopWhenFull = true, untilFull = false, fastUpscaler = true, gpuPack = true, gpVerify = false;
static int frames = 120, longFrames = 600, clipFrames = 120, writers = 1, autoStart = -1, slots = 128, recording = 0, recFrame = 0, dispatches = 0;

static const char* genv(const char* k) {      // CYBOCAP_<k>, or the older FSRDUMP_<k>
    char a[64]; snprintf(a, sizeof a, "CYBOCAP_%s", k); const char* v = getenv(a); if (v) return v;
    snprintf(a, sizeof a, "FSRDUMP_%s", k); return getenv(a);
}
static void logm(const char* fmt, ...) {
    if (!logf) return; va_list a; va_start(a, fmt); vfprintf(logf, fmt, a); va_end(a); fputc('\n', logf); fflush(logf);
}
static void init() {
    static bool done = false; if (done) return; done = true;
    char p[MAX_PATH]; GetModuleFileNameA(self, p, MAX_PATH); char* s = strrchr(p, '\\'); if (s) s[1] = 0;
    char r[MAX_PATH]; snprintf(r, MAX_PATH, "%s" REAL_DLL, p); real = LoadLibraryA(r);
    if (real) {
        pCreate = (PfnFfxCreateContext)GetProcAddress(real, "ffxCreateContext"); pDestroy = (PfnFfxDestroyContext)GetProcAddress(real, "ffxDestroyContext");
        pConfigure = (PfnFfxConfigure)GetProcAddress(real, "ffxConfigure"); pQuery = (PfnFfxQuery)GetProcAddress(real, "ffxQuery"); pDispatch = (PfnFfxDispatch)GetProcAddress(real, "ffxDispatch");
    }
    char ini[MAX_PATH], v[MAX_PATH]; snprintf(ini, MAX_PATH, "%scybocap.ini", p); bool haveIni = GetFileAttributesA(ini) != INVALID_FILE_ATTRIBUTES;
    if (!haveIni) { snprintf(ini, MAX_PATH, "%sfsrdump.ini", p); haveIni = GetFileAttributesA(ini) != INVALID_FILE_ATTRIBUTES; if (!haveIni) snprintf(ini, MAX_PATH, "%scybocap.ini", p); }
    char sec[16] = "cybocap"; { char t[8]; if (!GetPrivateProfileStringA("cybocap", nullptr, "", t, sizeof t, ini)) strcpy(sec, "fsrdump"); }      // [cybocap], or an older [fsrdump] section
    auto num = [&](const char* k, double def) { char b[64]; GetPrivateProfileStringA(sec, k, "", b, sizeof b, ini); return b[0] ? atof(b) : def; };
    shortSec = num("Seconds", shortSec); longSec = num("LongSeconds", longSec); longStopWhenFull = num("LongStopWhenFull", 1) != 0; fastUpscaler = num("FastUpscaler", 1) != 0; gpuPack = num("GpuPack", 1) != 0;
    if (genv("FAST_UPSCALER")) fastUpscaler = atoi(genv("FAST_UPSCALER")) != 0;
    if (genv("GPU_PACK")) gpuPack = atoi(genv("GPU_PACK")) != 0;
    gpVerify = num("GpuPackVerify", 0) != 0 || genv("GPU_PACK_VERIFY") != nullptr;      // tests: packed inputs are ALSO written raw (.bin next to .bin.gp) to check the decoder in a game
    writers = (int)num("Writers", writers); slots = (int)num("Slots", slots);
    GetPrivateProfileStringA(sec, "Folder", "", v, MAX_PATH, ini);
    const char* d = genv("DIR"); if (d) snprintf(base, MAX_PATH, "%s", d); else if (v[0]) snprintf(base, MAX_PATH, "%s", v); else snprintf(base, MAX_PATH, "%sclips", p);
    CreateDirectoryA(base, NULL);
    frames = (int)(shortSec * 60 + 0.5); longFrames = (int)(longSec * 60 + 0.5);
    if (genv("FRAMES")) frames = atoi(genv("FRAMES")); if (genv("LONG_FRAMES")) longFrames = atoi(genv("LONG_FRAMES"));
    if (genv("WRITERS")) writers = atoi(genv("WRITERS")); if (writers < 1) writers = 1; if (writers > 4) writers = 4; if (genv("AUTO")) autoStart = atoi(genv("AUTO"));
    if (genv("SLOTS")) slots = atoi(genv("SLOTS"));
    if (slots < 2) slots = 2; if (slots > 128) slots = 128; if (writers < 1) writers = 1; if (writers > 4) writers = 4;
    char lp[MAX_PATH]; snprintf(lp, MAX_PATH, "%s\\cybocap.log", base); logf = fopen(lp, "a");
    if (!logf) { snprintf(lp, MAX_PATH, "%scybocap.log", p); logf = fopen(lp, "a"); }
    logm("CyboCap 1.2 (FSR capture) loaded; real loader %s: %s; ini %s (%s); F10 %.1f s, Shift+F10 up to %.1f s%s; env frames %s; auto %d, slots %d, writers %d, FastUpscaler %d, GpuPack %d", r, real ? "ok" : "MISSING", ini, haveIni ? "found" : "not found, defaults",
         shortSec, longSec, longStopWhenFull ? " (stops before the game would wait)" : "", genv("FRAMES") ? genv("FRAMES") : "-", autoStart, slots, writers, (int)fastUpscaler, (int)gpuPack);
}

static D3D12_RESOURCE_STATES dxstate(uint32_t s) {
    D3D12_RESOURCE_STATES r = D3D12_RESOURCE_STATE_COMMON;
    if (s & FFX_API_RESOURCE_STATE_UNORDERED_ACCESS) r |= D3D12_RESOURCE_STATE_UNORDERED_ACCESS;
    if (s & FFX_API_RESOURCE_STATE_COMPUTE_READ) r |= D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE;
    if (s & FFX_API_RESOURCE_STATE_PIXEL_READ) r |= D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE;
    if (s & FFX_API_RESOURCE_STATE_COPY_SRC) r |= D3D12_RESOURCE_STATE_COPY_SOURCE;
    if (s & FFX_API_RESOURCE_STATE_COPY_DEST) r |= D3D12_RESOURCE_STATE_COPY_DEST;
    if (s & FFX_API_RESOURCE_STATE_INDIRECT_ARGUMENT) r |= D3D12_RESOURCE_STATE_INDIRECT_ARGUMENT;
    if (s & FFX_API_RESOURCE_STATE_RENDER_TARGET) r |= D3D12_RESOURCE_STATE_RENDER_TARGET;
    if (s & FFX_API_RESOURCE_STATE_DEPTH_ATTACHMENT) r |= D3D12_RESOURCE_STATE_DEPTH_WRITE;
    return r;
}

static const char* NAMES[6] = {"color", "depth", "motion", "exposure", "reactive", "tc"};
struct Img { ID3D12Resource* rb = nullptr; UINT64 rbSize = 0; D3D12_PLACED_SUBRESOURCE_FOOTPRINT fp = {}; UINT rows = 0; UINT64 rowSize = 0; bool used = false; DXGI_FORMAT fmt = DXGI_FORMAT_UNKNOWN; bool gp = false; UINT64 gpWords = 0; ID3D12Resource* rb2 = nullptr; UINT64 rb2Size = 0; bool both = false; };

struct Slot { Img img[6]; ID3D12Resource* cUp = nullptr; ID3D12Resource* cRb = nullptr; int frame = -1; volatile long state = 0; bool pending = false; char params[4096]; char dir[MAX_PATH]; };

static Slot ring[128];

static ID3D12Resource* buffer(ID3D12Device* dev, UINT64 size, D3D12_HEAP_TYPE heap, D3D12_RESOURCE_STATES st) {
    D3D12_HEAP_PROPERTIES hp = {}; hp.Type = heap; D3D12_RESOURCE_DESC rd = {}; rd.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER; rd.Width = size; rd.Height = 1;
    rd.DepthOrArraySize = 1; rd.MipLevels = 1; rd.SampleDesc.Count = 1; rd.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR; ID3D12Resource* r = nullptr;
    if (FAILED(dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &rd, st, nullptr, __uuidof(ID3D12Resource), (void**)&r))) return nullptr;
    return r;
}

// GpuPack (default 1): color, depth and motion are packed losslessly on the GPU (shaders/gpupack.hlsl) before the readback, about half the bytes.
static int gpCode(int i, DXGI_FORMAT f) {        // the shader's FMT for input i, or -1 = stored raw
    if (!gpuPack || i > 2) return -1;
    switch ((int)f) { case 26: return 0; case 44: case 45: case 46: return 1; case 16: return 2; case 39: case 40: case 41: return 3; case 10: return 4; case 34: return 5; default: return -1; }
}
static UINT64 gpCapacity(int code, UINT W, UINT H) {     // words: header + index + worst-case records
    UINT64 tiles = (UINT64)((W + 7) / 8) * ((H + 7) / 8); UINT bits = code == 0 ? 32 : code == 1 ? 32 : code == 2 ? 64 : code == 3 ? 32 : code == 4 ? 64 : 32;
    UINT nc = code == 0 ? 3 : code == 4 ? 4 : (code == 3 ? 1 : 2);
    return 8 + tiles * (1 + 2 + 2 * bits + nc);
}
static bool ensureRb(ID3D12Device* dev, Img& m, ID3D12Resource* r, UINT64* totalOut, int i = -1) {
    D3D12_RESOURCE_DESC rd; r->GetDesc(&rd); UINT64 total = 0;
    dev->GetCopyableFootprints(&rd, 0, 1, 0, &m.fp, &m.rows, &m.rowSize, &total);
    int code = i >= 0 ? gpCode(i, rd.Format) : -1; m.gp = code >= 0; m.gpWords = m.gp ? gpCapacity(code, (UINT)rd.Width, rd.Height) : 0;
    if (m.gp) total = m.gpWords * 4;
    if (totalOut) *totalOut = total;
    if (!m.rb || m.rbSize < total) { if (m.rb) m.rb->Release(); m.rb = buffer(dev, total, D3D12_HEAP_TYPE_READBACK, D3D12_RESOURCE_STATE_COPY_DEST); m.rbSize = m.rb ? total : 0; }
    return m.rb != nullptr;
}
struct GpSet { ID3D12Resource* gin = nullptr; ID3D12Resource* gout = nullptr; UINT64 ginSize = 0, goutSize = 0; };
static struct { ID3D12RootSignature* rs = nullptr; ID3D12PipelineState* pso = nullptr; bool tried = false, ok = false;
                GpSet set[6][3]; ID3D12Resource* init[6] = {}; UINT initW[6] = {}, initH[6] = {}; int initCode[6] = {-1, -1, -1, -1, -1, -1}; } gp;
static bool gpInit(ID3D12Device* dev) {
    if (gp.tried) return gp.ok; gp.tried = true;
    D3D12_ROOT_PARAMETER rp[3] = {};
    rp[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS; rp[0].Constants.ShaderRegister = 0; rp[0].Constants.Num32BitValues = 6;
    rp[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_SRV; rp[1].Descriptor.ShaderRegister = 0;
    rp[2].ParameterType = D3D12_ROOT_PARAMETER_TYPE_UAV; rp[2].Descriptor.ShaderRegister = 0;
    for (auto& r : rp) r.ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
    D3D12_ROOT_SIGNATURE_DESC rsd = {}; rsd.NumParameters = 3; rsd.pParameters = rp;
    ID3DBlob* blob = nullptr; ID3DBlob* err = nullptr;
    if (FAILED(D3D12SerializeRootSignature(&rsd, D3D_ROOT_SIGNATURE_VERSION_1, &blob, &err)) ||
        FAILED(dev->CreateRootSignature(0, blob->GetBufferPointer(), blob->GetBufferSize(), __uuidof(ID3D12RootSignature), (void**)&gp.rs))) {
        logm("GpuPack: root signature failed: inputs stored raw"); if (blob) blob->Release(); if (err) err->Release(); return false;
    }
    blob->Release();
    D3D12_COMPUTE_PIPELINE_STATE_DESC pd = {}; pd.pRootSignature = gp.rs; pd.CS.pShaderBytecode = g_gpupack; pd.CS.BytecodeLength = sizeof g_gpupack;
    if (FAILED(dev->CreateComputePipelineState(&pd, __uuidof(ID3D12PipelineState), (void**)&gp.pso))) { logm("GpuPack: compute pipeline failed: inputs stored raw"); return false; }
    gp.ok = true; logm("GpuPack: ready (color, depth and motion are packed on the GPU before the readback)"); return true;
}
static bool gpEnsure(ID3D12Device* dev, int i, int set, UINT64 ginSize, UINT64 goutSize, int code, UINT W, UINT H) {
    GpSet& g = gp.set[i][set];
    if (!g.gin || g.ginSize < ginSize) { if (g.gin) g.gin->Release(); g.gin = buffer(dev, ginSize, D3D12_HEAP_TYPE_DEFAULT, D3D12_RESOURCE_STATE_COPY_DEST); g.ginSize = g.gin ? ginSize : 0; }
    if (!g.gout || g.goutSize < goutSize) {
        if (g.gout) g.gout->Release();
        D3D12_HEAP_PROPERTIES hp = {}; hp.Type = D3D12_HEAP_TYPE_DEFAULT; D3D12_RESOURCE_DESC rd = {}; rd.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER; rd.Width = goutSize;
        rd.Height = 1; rd.DepthOrArraySize = 1; rd.MipLevels = 1; rd.SampleDesc.Count = 1; rd.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR; rd.Flags = D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS;
        g.gout = nullptr; if (FAILED(dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &rd, D3D12_RESOURCE_STATE_COPY_DEST, nullptr, __uuidof(ID3D12Resource), (void**)&g.gout))) g.gout = nullptr;
        g.goutSize = g.gout ? goutSize : 0;
    }
    if (!gp.init[i] || gp.initW[i] != W || gp.initH[i] != H || gp.initCode[i] != code) {      // the 8 header words copied into the output first
        if (!gp.init[i]) gp.init[i] = buffer(dev, 256, D3D12_HEAP_TYPE_UPLOAD, D3D12_RESOURCE_STATE_GENERIC_READ);
        uint32_t* w = nullptr; D3D12_RANGE r0 = {0, 0};
        if (gp.init[i] && SUCCEEDED(gp.init[i]->Map(0, &r0, (void**)&w))) {
            UINT tx = (W + 7) / 8, ty = (H + 7) / 8; w[0] = 8 + tx * ty; w[1] = 0x50475943u; w[2] = 1; w[3] = W; w[4] = H; w[5] = (uint32_t)code; w[6] = tx; w[7] = ty;
            gp.init[i]->Unmap(0, nullptr); gp.initW[i] = W; gp.initH[i] = H; gp.initCode[i] = code;
        }
    }
    return g.gin && g.gout && gp.init[i];
}
static DWORD waitMs = 0;
static std::deque<int> q; static std::mutex qm; static std::condition_variable qc; static int inflight = 0;
static void writeSlot(Slot& s) {
    thread_local std::vector<uint8_t> stage; char fn[MAX_PATH];
    for (int i = 0; i < 6; i++) {
        Img& m = s.img[i]; if (!m.used) continue; uint8_t* p = nullptr; D3D12_RANGE r = {0, (SIZE_T)m.rbSize};
        if (FAILED(m.rb->Map(0, &r, (void**)&p))) { logm("frame %d %s: map failed", s.frame, NAMES[i]); continue; }
        snprintf(fn, MAX_PATH, "%s\\frame_%03d_%s_%ux%u_f%d.bin%s", s.dir, s.frame, NAMES[i], m.fp.Footprint.Width, m.rows, (int)m.fmt, m.gp ? ".gp" : "");
        if (m.gp) {                                              // GPU-packed: word 0 = words used; write exactly those words
            uint32_t used = *(uint32_t*)p;
            if (used < 8 || used > m.gpWords) { logm("frame %d %s: packed size %u out of range (capacity %llu): skipped", s.frame, NAMES[i], used, (unsigned long long)m.gpWords); D3D12_RANGE w0 = {0, 0}; m.rb->Unmap(0, &w0); continue; }
            size_t bytes = (size_t)used * 4; if (stage.size() < bytes) stage.resize(bytes); memcpy(stage.data(), p, bytes);
            { D3D12_RANGE w0 = {0, 0}; m.rb->Unmap(0, &w0); }
            HANDLE h = CreateFileA(fn, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
            if (h != INVALID_HANDLE_VALUE) { DWORD w = 0; if (!WriteFile(h, stage.data(), (DWORD)bytes, &w, NULL) || w != bytes) logm("write failed %s", fn); CloseHandle(h); } else logm("cannot write %s", fn);
            D3D12_RANGE r2 = {0, (SIZE_T)m.rb2Size};
            if (m.both && SUCCEEDED(m.rb2->Map(0, &r2, (void**)&p))) {    // verify mode: the raw rows too
                size_t rb = (size_t)m.rowSize * m.rows; if (stage.size() < rb) stage.resize(rb);
                for (UINT y = 0; y < m.rows; y++) memcpy(stage.data() + (size_t)y * m.rowSize, p + (UINT64)y * m.fp.Footprint.RowPitch, (size_t)m.rowSize);
                { D3D12_RANGE w0 = {0, 0}; m.rb2->Unmap(0, &w0); }
                fn[strlen(fn) - 3] = 0; HANDLE h2 = CreateFileA(fn, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
                if (h2 != INVALID_HANDLE_VALUE) { DWORD w = 0; WriteFile(h2, stage.data(), (DWORD)rb, &w, NULL); CloseHandle(h2); }
            }
            continue;
        }

        size_t bytes = (size_t)m.rowSize * m.rows; if (stage.size() < bytes) stage.resize(bytes);
        if (m.fp.Footprint.RowPitch == m.rowSize) memcpy(stage.data(), p + m.fp.Offset, bytes);
        else for (UINT y = 0; y < m.rows; y++) memcpy(stage.data() + (size_t)y * m.rowSize, p + m.fp.Offset + (UINT64)y * m.fp.Footprint.RowPitch, (size_t)m.rowSize);
        { D3D12_RANGE w0 = {0, 0}; m.rb->Unmap(0, &w0); }
        const uint8_t* src = stage.data();
        HANDLE h = CreateFileA(fn, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
        if (h != INVALID_HANDLE_VALUE) {
            size_t off = 0; while (off < bytes) { DWORD w = 0, chunk = (DWORD)std::min<size_t>(bytes - off, (size_t)1 << 30); if (!WriteFile(h, src + off, chunk, &w, NULL) || !w) { logm("write failed %s", fn); break; } off += w; }
            CloseHandle(h);
        } else logm("cannot write %s", fn);
    }
    snprintf(fn, MAX_PATH, "%s\\frame_%03d.txt", s.dir, s.frame); FILE* f = fopen(fn, "w"); if (f) { fputs(s.params, f); fclose(f); }
}
static void writer() {
    for (;;) {
        int i; bool idle; { std::unique_lock<std::mutex> l(qm); qc.wait(l, [] { return !q.empty(); }); i = q.front(); q.pop_front(); inflight++; }
        writeSlot(ring[i]); { std::lock_guard<std::mutex> l(qm); inflight--; idle = q.empty() && inflight == 0; } InterlockedExchange(&ring[i].state, 0);
        if (idle && !recording) logm("all queued files written (last frame %d)", ring[i].frame);
    }
}
static void handOver(int i) {
    static bool started = false; if (!started) { started = true; for (int t = 0; t < writers; t++) std::thread(writer).detach(); }
    InterlockedExchange(&ring[i].state, 2); { std::lock_guard<std::mutex> l(qm); q.push_back(i); } qc.notify_one();
}
static bool gpuDone(Slot& s) {
    uint32_t* c = nullptr; D3D12_RANGE rr = {0, 4}; bool ok = false;
    if (SUCCEEDED(s.cRb->Map(0, &rr, (void**)&c))) { ok = *c == (uint32_t)s.frame + 1; D3D12_RANGE w0 = {0, 0}; s.cRb->Unmap(0, &w0); }
    return ok;
}
static bool flush(int i, bool wait) {
    Slot& s = ring[i]; DWORD t0 = GetTickCount();
    for (;;) {
        if (s.state == 0) return true;
        if (s.state == 1 && gpuDone(s)) handOver(i);
        if (!wait) return s.state == 0;
        if (s.state == 1 && GetTickCount() - t0 > 3000) { logm("frame %d: canary not reached in 3 s, data skipped", s.frame); InterlockedExchange(&s.state, 0); return true; }
        Sleep(1);
    }
}
static double profA, profB, profC, profD; static LARGE_INTEGER pfq;
static double pnow() { LARGE_INTEGER t; if (!pfq.QuadPart) QueryPerformanceFrequency(&pfq); QueryPerformanceCounter(&t); return (double)t.QuadPart * 1000.0 / pfq.QuadPart; }
static void capture(const ffxDispatchDescUpscale* d) {
    ID3D12GraphicsCommandList* cl = (ID3D12GraphicsCommandList*)d->commandList; if (!cl) return;
    int si = recFrame % slots; Slot& s = ring[si]; DWORD tw = GetTickCount(); flush(si, true); waitMs += GetTickCount() - tw;
    const FfxApiResource* in[6] = {&d->color, &d->depth, &d->motionVectors, &d->exposure, &d->reactive, &d->transparencyAndComposition};
    ID3D12Device* dev = nullptr; int n = snprintf(s.params, sizeof s.params, "frame %d dispatch %d\n", recFrame, dispatches);
    double c0 = pnow();
    for (int i = 0; i < 6; i++) {
        Img& m = s.img[i]; m.used = false; ID3D12Resource* r = (ID3D12Resource*)in[i]->resource; if (!r) continue;
        if (!dev && FAILED(r->GetDevice(__uuidof(ID3D12Device), (void**)&dev))) return;
        D3D12_RESOURCE_DESC rd; r->GetDesc(&rd); UINT64 total = 0;
        if (!ensureRb(dev, m, r, &total, i)) { logm("frame %d %s: readback buffer of %llu bytes failed", recFrame, NAMES[i], (unsigned long long)total); continue; }
        int code = m.gp ? gpCode(i, rd.Format) : -1; int set = recFrame % 3; UINT64 ginSize = (UINT64)m.fp.Footprint.RowPitch * m.rows;
        if (code >= 0 && !(gpInit(dev) && gpEnsure(dev, i, set, ginSize, m.gpWords * 4, code, (UINT)rd.Width, rd.Height))) {
            static bool told = false; if (!told) { told = true; logm("GpuPack unavailable for %s: stored raw", NAMES[i]); }
            code = -1; m.gp = false; ensureRb(dev, m, r, &total, -1);
        }
        D3D12_RESOURCE_STATES st = dxstate(in[i]->state); bool tr = !(st & D3D12_RESOURCE_STATE_COPY_SOURCE);
        D3D12_RESOURCE_BARRIER b = {}; b.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION; b.Transition.pResource = r; b.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
        b.Transition.StateBefore = st; b.Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_SOURCE; if (tr) cl->ResourceBarrier(1, &b);
        D3D12_TEXTURE_COPY_LOCATION src = {}, dst = {}; src.pResource = r; src.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX; src.SubresourceIndex = 0;
        GpSet* g = code >= 0 ? &gp.set[i][set] : nullptr;
        dst.pResource = g ? g->gin : m.rb; dst.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT; dst.PlacedFootprint = m.fp; if (g) dst.PlacedFootprint.Offset = 0;
        cl->CopyTextureRegion(&dst, 0, 0, 0, &src, nullptr);
        m.both = false;
        if (g && gpVerify) {
            UINT64 need = (UINT64)m.fp.Footprint.RowPitch * m.rows;
            if (!m.rb2 || m.rb2Size < need) { if (m.rb2) m.rb2->Release(); m.rb2 = buffer(dev, need, D3D12_HEAP_TYPE_READBACK, D3D12_RESOURCE_STATE_COPY_DEST); m.rb2Size = m.rb2 ? need : 0; }
            if (m.rb2) { D3D12_TEXTURE_COPY_LOCATION d2 = dst; d2.pResource = m.rb2; cl->CopyTextureRegion(&d2, 0, 0, 0, &src, nullptr); m.both = true; }
        }
        b.Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_SOURCE; b.Transition.StateAfter = st; if (tr) cl->ResourceBarrier(1, &b);
        if (g) {                                                  // pack on the GPU: header words, dispatch, copy the result to the readback buffer
            cl->CopyBufferRegion(g->gout, 0, gp.init[i], 0, 32);
            D3D12_RESOURCE_BARRIER bb[2] = {};
            for (auto& x : bb) { x.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION; x.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES; }
            bb[0].Transition.pResource = g->gin; bb[0].Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_DEST; bb[0].Transition.StateAfter = D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE;
            bb[1].Transition.pResource = g->gout; bb[1].Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_DEST; bb[1].Transition.StateAfter = D3D12_RESOURCE_STATE_UNORDERED_ACCESS;
            cl->ResourceBarrier(2, bb);
            UINT W = (UINT)rd.Width, H = rd.Height, tx = (W + 7) / 8, tiles = tx * ((H + 7) / 8);
            uint32_t k[6] = {W, H, (uint32_t)code, (uint32_t)m.fp.Footprint.RowPitch, tx, 8u};
            cl->SetComputeRootSignature(gp.rs); cl->SetPipelineState(gp.pso); cl->SetComputeRoot32BitConstants(0, 6, k, 0);
            cl->SetComputeRootShaderResourceView(1, g->gin->GetGPUVirtualAddress()); cl->SetComputeRootUnorderedAccessView(2, g->gout->GetGPUVirtualAddress());
            cl->Dispatch(tiles < 65535u ? tiles : 65535u, (tiles + 65534u) / 65535u, 1);
            bb[0].Transition.StateBefore = D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE; bb[0].Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_DEST;
            bb[1].Transition.StateBefore = D3D12_RESOURCE_STATE_UNORDERED_ACCESS; bb[1].Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_SOURCE;
            cl->ResourceBarrier(2, bb);
            cl->CopyBufferRegion(m.rb, 0, g->gout, 0, m.gpWords * 4);
            bb[1].Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_SOURCE; bb[1].Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_DEST; cl->ResourceBarrier(1, &bb[1]);
        }
        m.used = true; m.fmt = rd.Format;
        n += snprintf(s.params + n, sizeof s.params - n, "%s %llux%u format %d state %u copied_rows %u row_bytes %llu%s\n", NAMES[i], (unsigned long long)rd.Width, rd.Height, (int)rd.Format, in[i]->state, m.rows, (unsigned long long)m.rowSize, m.gp ? " gpupack 1" : "");
    }
    profA = pnow() - c0; c0 = pnow();
    if (!dev) return;
    if (!s.cUp) { s.cUp = buffer(dev, 256, D3D12_HEAP_TYPE_UPLOAD, D3D12_RESOURCE_STATE_GENERIC_READ); s.cRb = buffer(dev, 256, D3D12_HEAP_TYPE_READBACK, D3D12_RESOURCE_STATE_COPY_DEST); }
    uint32_t* c = nullptr; D3D12_RANGE r0 = {0, 0}; if (s.cRb && SUCCEEDED(s.cRb->Map(0, &r0, (void**)&c))) { *c = 0; s.cRb->Unmap(0, nullptr); }
    if (s.cUp && SUCCEEDED(s.cUp->Map(0, &r0, (void**)&c))) { *c = (uint32_t)recFrame + 1; s.cUp->Unmap(0, nullptr); }
    cl->CopyBufferRegion(s.cRb, 0, s.cUp, 0, 4); dev->Release(); profB = pnow() - c0;
    n += snprintf(s.params + n, sizeof s.params - n,
        "jitter %.9g %.9g\nmotionVectorScale %.9g %.9g\nrenderSize %u %u\nupscaleSize %u %u\nenableSharpening %d sharpness %.9g\nframeTimeDelta %.9g\npreExposure %.9g\n"
        "reset %d\ncameraNear %.9g cameraFar %.9g fovV %.9g viewSpaceToMeters %.9g\nflags %u\n",
        d->jitterOffset.x, d->jitterOffset.y, d->motionVectorScale.x, d->motionVectorScale.y, d->renderSize.width, d->renderSize.height, d->upscaleSize.width,
        d->upscaleSize.height, (int)d->enableSharpening, d->sharpness, d->frameTimeDelta, d->preExposure, (int)d->reset, d->cameraNear, d->cameraFar,
        d->cameraFovAngleVertical, d->viewSpaceToMetersFactor, d->flags);
    s.frame = recFrame; snprintf(s.dir, MAX_PATH, "%s", clip); InterlockedExchange(&s.state, 1);
}

static char lastCreate[256];
static int startDelay = 0;
static void prealloc(const ffxDispatchDescUpscale* d) {
    const FfxApiResource* in[6] = {&d->color, &d->depth, &d->motionVectors, &d->exposure, &d->reactive, &d->transparencyAndComposition};
    DWORD t0 = GetTickCount(); ID3D12Device* dev = nullptr; int ok = 0;
    for (int i = 0; i < 6 && !dev; i++) if (in[i]->resource) ((ID3D12Resource*)in[i]->resource)->GetDevice(__uuidof(ID3D12Device), (void**)&dev);
    if (!dev) return;
    for (int k = 0; k < slots; k++) {
        Slot& s = ring[k]; flush(k, true); bool all = true;
        for (int i = 0; i < 6; i++) if (in[i]->resource) {
            Img& m = s.img[i]; all &= ensureRb(dev, m, (ID3D12Resource*)in[i]->resource, nullptr, i);
            if (gpVerify && m.gp) {                               // GpuPackVerify: the raw copies are allocated here too, not during the clip
                UINT64 need = (UINT64)m.fp.Footprint.RowPitch * m.rows;
                if (!m.rb2 || m.rb2Size < need) { if (m.rb2) m.rb2->Release(); m.rb2 = buffer(dev, need, D3D12_HEAP_TYPE_READBACK, D3D12_RESOURCE_STATE_COPY_DEST); m.rb2Size = m.rb2 ? need : 0; }
            }
        }
        if (!s.cUp) { s.cUp = buffer(dev, 256, D3D12_HEAP_TYPE_UPLOAD, D3D12_RESOURCE_STATE_GENERIC_READ); s.cRb = buffer(dev, 256, D3D12_HEAP_TYPE_READBACK, D3D12_RESOURCE_STATE_COPY_DEST); }
        ok += all && s.cRb;
    }
    if (gpuPack) gpInit(dev);
    DWORD pt = GetTickCount() - t0; dev->Release(); logm("prealloc: %d of %d slots ready in %lu ms", ok, slots, (unsigned long)pt);

    startDelay = pt > 50 ? 4 : 0;
}

extern "C" {
BOOL WINAPI DllMain(HINSTANCE h, DWORD why, LPVOID) { if (why == DLL_PROCESS_ATTACH) self = h; return TRUE; }
__declspec(dllexport) ffxReturnCode_t ffxCreateContext(ffxContext* c, ffxCreateContextDescHeader* d, const ffxAllocationCallbacks* m) {
    init();
    for (ffxApiHeader* h = d; h; h = h->pNext)
        if (h->type == FFX_API_CREATE_CONTEXT_DESC_TYPE_UPSCALE) {
            const ffxCreateContextDescUpscale* u = (const ffxCreateContextDescUpscale*)h;
            logm("create upscale context: flags 0x%x maxRender %ux%u maxUpscale %ux%u", u->flags, u->maxRenderSize.width, u->maxRenderSize.height, u->maxUpscaleSize.width, u->maxUpscaleSize.height);
            char fn[MAX_PATH]; snprintf(fn, MAX_PATH, "%s\\create.txt", base); FILE* f = fopen(fn, "a");
            snprintf(lastCreate, sizeof lastCreate, "flags 0x%x maxRender %ux%u maxUpscale %ux%u\n", u->flags, u->maxRenderSize.width, u->maxRenderSize.height, u->maxUpscaleSize.width, u->maxUpscaleSize.height);
            if (f) { fputs(lastCreate, f); fclose(f); }
        }
    // FastUpscaler (default 1): the clip holds the game's inputs, which are the same whichever FSR version runs, so while CyboCap is installed the
    // upscale context runs the fastest version in the real DLL (FSR 3.1.x: no machine learning, fastest on every GPU), not the one OptiScaler asked for.
    ffxApiHeader* up = nullptr; ID3D12Device* dev = nullptr; ffxOverrideVersion* ov = nullptr;
    for (ffxApiHeader* h = d; h; h = h->pNext) {
        if (h->type == FFX_API_CREATE_CONTEXT_DESC_TYPE_UPSCALE) up = h;
        else if (h->type == FFX_API_CREATE_CONTEXT_DESC_TYPE_BACKEND_DX12) dev = ((ffxCreateBackendDX12Desc*)h)->device;
        else if (h->type == FFX_API_DESC_TYPE_OVERRIDE_VERSION) ov = (ffxOverrideVersion*)h;
    }
    uint64_t pick = 0; const char* pickName = nullptr;
    if (fastUpscaler && up && dev && pQuery) {
        uint64_t n = 16, ids[16] = {}; const char* names[16] = {};
        ffxQueryDescGetVersions q = {}; q.header.type = FFX_API_QUERY_DESC_TYPE_GET_VERSIONS; q.createDescType = FFX_API_CREATE_CONTEXT_DESC_TYPE_UPSCALE;
        q.device = dev; q.outputCount = &n; q.versionIds = ids; q.versionNames = names;
        if (pQuery(nullptr, &q.header) == FFX_API_RETURN_OK) {
            char list[512] = ""; int len = 0;
            for (uint64_t i = 0; i < n && i < 16; i++) {
                const char* nm = names[i] ? names[i] : "?"; len += snprintf(list + len, sizeof list - len, "%s%s", i ? ", " : "", nm);
                if (nm[0] == '3' && nm[1] == '.' && (!pickName || strcmp(nm, pickName) > 0)) { pick = ids[i]; pickName = nm; }
            }
            logm("upscaler versions in the real DLL: %s", list);
        } else logm("upscaler version query failed: the game's choice is kept");
    }
    if (!pick) return pCreate ? pCreate(c, d, m) : FFX_API_RETURN_ERROR;
    ffxReturnCode_t rc;
    if (ov) { uint64_t was = ov->versionId; ov->versionId = pick; rc = pCreate ? pCreate(c, d, m) : FFX_API_RETURN_ERROR; ov->versionId = was; }      // replaces OptiScaler's pick for this call
    else {
        ffxOverrideVersion mine = {}; mine.header.type = FFX_API_DESC_TYPE_OVERRIDE_VERSION; mine.header.pNext = d->pNext; d->pNext = &mine.header;
        rc = pCreate ? pCreate(c, d, m) : FFX_API_RETURN_ERROR; d->pNext = mine.header.pNext;
    }
    logm("upscale context runs FSR %s for speed (FastUpscaler=1; the clips are the same with any version): %s", pickName, rc == FFX_API_RETURN_OK ? "ok" : "FAILED");
    if (rc != FFX_API_RETURN_OK) { logm("retrying with the game's choice"); rc = pCreate ? pCreate(c, d, m) : FFX_API_RETURN_ERROR; }
    return rc;
}
__declspec(dllexport) ffxReturnCode_t ffxDestroyContext(ffxContext* c, const ffxAllocationCallbacks* m) { init(); return pDestroy ? pDestroy(c, m) : FFX_API_RETURN_ERROR; }
__declspec(dllexport) ffxReturnCode_t ffxConfigure(ffxContext* c, const ffxConfigureDescHeader* d) { init(); return pConfigure ? pConfigure(c, d) : FFX_API_RETURN_ERROR; }
__declspec(dllexport) ffxReturnCode_t ffxQuery(ffxContext* c, ffxQueryDescHeader* d) { init(); return pQuery ? pQuery(c, d) : FFX_API_RETURN_ERROR; }
__declspec(dllexport) ffxReturnCode_t ffxDispatch(ffxContext* c, const ffxDispatchDescHeader* d) {
    init();

    static const bool prof = genv("PROFILE") != nullptr; static LARGE_INTEGER pf, tPrev; if (prof && !pf.QuadPart) QueryPerformanceFrequency(&pf);
    LARGE_INTEGER tA; if (prof) QueryPerformanceCounter(&tA);
    for (const ffxApiHeader* h = d; h; h = h->pNext)
        if (h->type == FFX_API_DISPATCH_DESC_TYPE_UPSCALE) {
            dispatches++;

            static bool f10prev = false; const bool f10 = (GetAsyncKeyState(VK_F10) & 0x8000) != 0; bool key = f10 && !f10prev; f10prev = f10;
            static int autoStop = genv("AUTO_STOP") ? atoi(genv("AUTO_STOP")) : -1; if (dispatches == autoStop) key = true;      // tests: a press at dispatch n
            const bool shift = (GetAsyncKeyState(VK_SHIFT) & 0x8000) != 0;

            // 1.2: F10 or Shift+F10 during a clip ends it after the frames recorded so far (they are written as a complete clip)
            if (recording && key) {
                if (recFrame < clipFrames) { logm("stopped by key at frame %d (of up to %d)", recFrame, clipFrames); clipFrames = recFrame; startDelay = 0; }
                key = false;
            }
            bool busy = false; if (!recording && key) { std::lock_guard<std::mutex> l(qm); busy = !q.empty(); for (int i = 0; i < slots && !busy; i++) busy = ring[i].state != 0; }
            if (busy) { logm("F10 ignored: the last clip is still being written (dispatch %d)", dispatches); key = false; }
            if (!recording && (key || dispatches == autoStart)) {
                SYSTEMTIME t; GetLocalTime(&t); snprintf(clip, MAX_PATH, "%s\\clip_%04d%02d%02d_%02d%02d%02d", base, t.wYear, t.wMonth, t.wDay, t.wHour, t.wMinute, t.wSecond);
                { const ffxDispatchDescUpscale* u = (const ffxDispatchDescUpscale*)h; double ft = u->frameTimeDelta > 1 && u->frameTimeDelta < 200 ? u->frameTimeDelta : 1000.0 / 60;

                  bool lng = (key && shift) || (!key && genv("AUTO_LONG")); untilFull = lng && longStopWhenFull;
                  if (lng) clipFrames = genv("LONG_FRAMES") ? longFrames : (int)(longSec * 1000 / ft + 0.5);
                  else clipFrames = genv("FRAMES") ? frames : (int)(shortSec * 1000 / ft + 0.5);
                  if (clipFrames < 1) clipFrames = 1; }
                CreateDirectoryA(clip, NULL);
                { char fn[MAX_PATH], exe[MAX_PATH]; GetModuleFileNameA(NULL, exe, MAX_PATH); const char* b = strrchr(exe, '\\'); snprintf(fn, MAX_PATH, "%s\\clip.txt", clip);
                  FILE* cf = fopen(fn, "w"); if (cf) { fprintf(cf, "%sgame %s\nCyboCap 1.2\n", lastCreate, b ? b + 1 : exe); fclose(cf); } }
                prealloc((const ffxDispatchDescUpscale*)h); recording = 1; recFrame = 0; waitMs = 0; logm("recording %d frames into %s (dispatch %d)", clipFrames, clip, dispatches);
            }
            if (recording && startDelay > 0) { if (--startDelay == 0) logm("recording starts now (dispatch %d), after the allocation pause", dispatches); }
            else if (recording && recFrame < clipFrames) {
                double u0 = pnow(); bool freeSlot = !untilFull || flush(recFrame % slots, false); profD = pnow() - u0;
                if (!freeSlot) { logm("readback ring full at frame %d: long clip ends here so the game does not wait", recFrame); clipFrames = recFrame; }
                else { capture((const ffxDispatchDescUpscale*)h); recFrame++; }
            }
            { double f0 = pnow();
            for (int i = 0; i < slots; i++) flush(i, false);
              profC = pnow() - f0; }
            if (recording && recFrame >= clipFrames) {
                bool all = true; for (int i = 0; i < slots; i++) all &= ring[i].state != 1;
                if (all) { recording = 0; logm("clip done: %s (%d frames, game waited %lu ms for free slots)", clip, clipFrames, (unsigned long)waitMs); }
            }
        }
    LARGE_INTEGER tB; if (prof) QueryPerformanceCounter(&tB);
    ffxReturnCode_t rc = pDispatch ? pDispatch(c, d) : FFX_API_RETURN_ERROR;
    if (prof && recording) { LARGE_INTEGER tC; QueryPerformanceCounter(&tC); auto ms = [](LARGE_INTEGER a, LARGE_INTEGER b) { return (double)(b.QuadPart - a.QuadPart) * 1000.0 / pf.QuadPart; };
        logm("prof frame %d: ours %.2f ms (images %.2f, canary %.2f, flush-all %.2f, full-check %.2f, waited total %lu), real %.2f ms, since last %.2f ms", recFrame, ms(tA, tB), profA, profB, profC, profD, (unsigned long)waitMs, ms(tB, tC), tPrev.QuadPart ? ms(tPrev, tA) : 0.0); }
    if (prof) tPrev = tA;
    return rc;
}
}
