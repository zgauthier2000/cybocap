#!/usr/bin/env bash
# build.sh: builds amd_fidelityfx_upscaler_dx12.dll (the capture proxy) with an llvm-mingw toolchain (https://github.com/mstorsjo/llvm-mingw).
# Usage: MINGW=<llvm-mingw>/bin ./build.sh   -> out/amd_fidelityfx_upscaler_dx12.dll
set -euo pipefail; cd "$(dirname "$0")"; B=${MINGW:?set MINGW to the llvm-mingw bin folder}; mkdir -p out
"$B/x86_64-w64-mingw32-windres" src/version.rc -O coff -o out/version.o
"$B/x86_64-w64-mingw32-clang++" -O2 -std=c++17 -shared -static -Ithird_party/ffx/api/include -Ithird_party/ffx/upscalers/include \
    -o out/amd_fidelityfx_upscaler_dx12.dll src/cybocap.cpp out/version.o -ld3d12
echo "built out/amd_fidelityfx_upscaler_dx12.dll"
