#!/bin/sh
# Build ssoverlay for the A810Pro (ARMv7 Cortex-A9, static, no libc) and the host harness.
# Needs Homebrew llvm + lld (or set LLVM / LLD), and a copy of main_app in
# MAIN_APP_ELF to regenerate font_data.h and fingerprint.h.
set -e
cd "$(dirname "$0")"
LLVM=${LLVM:-/opt/homebrew/opt/llvm/bin}
LLD=${LLD:-/opt/homebrew/opt/lld/bin}
python3 -I gen_font.py font_data.h
python3 -I gen_fingerprint.py fingerprint.h
"$LLVM/clang" --target=armv7a-linux-gnueabihf -mcpu=cortex-a9 -mfloat-abi=hard -mfpu=neon \
  -O2 -Wall -Wextra -ffreestanding -fno-builtin -fno-stack-protector -fno-pic -fno-pie \
  -fno-unwind-tables -fno-asynchronous-unwind-tables -nostdlib -static \
  --ld-path="$LLD/ld.lld" -Wl,-e,_start -Wl,--gc-sections -Wl,-z,norelro \
  -o ssoverlay ssoverlay.c
"$LLVM/llvm-strip" ssoverlay 2>/dev/null || true
cc -O2 -Wall -DHOST -o ssoverlay_host ssoverlay.c
file ssoverlay ssoverlay_host
ls -la ssoverlay
