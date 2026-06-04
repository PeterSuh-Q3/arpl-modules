#!/usr/bin/env python3
"""
add_serial_probe.py — 커널 진입점 4곳에 COM1 serial probe 삽입.

Probe 문자:
  'K' @ startup_64       (0xffffffff8100007e) — 이미 삽입됨 (v1)
  'X' @ x86_64_start_kernel (0xffffffff82a1a3bb)
  'S' @ start_kernel        (0xffffffff82a1aa22)
  'A' @ setup_arch          (0xffffffff82a21afc)

v2: startup_64 probe 유지 + X/S/A 추가

Usage: python3 add_serial_probe.py <input.gz> <output.gz>
"""
import sys, struct, gzip, hashlib

def u32(b,o): return struct.unpack_from('<I',b,o)[0]
def u64(b,o): return struct.unpack_from('<Q',b,o)[0]

def load_gz(path):
    with gzip.open(path, 'rb') as f:
        return bytearray(f.read())

def save_gz(path, data):
    with gzip.open(path, 'wb', compresslevel=6) as f:
        f.write(data)

if len(sys.argv) != 3:
    print("Usage: python3 add_serial_probe.py <input.gz> <output.gz>")
    sys.exit(1)

IN, OUT = sys.argv[1], sys.argv[2]
print("Loading %s ..." % IN)
data = load_gz(IN)

# --- ELF 파싱: 모든 LOAD segment 수집 ---
setup_sects = data[0x1f1]
kernel_offset = (setup_sects + 1) * 512
payload_off = u32(data, 0x248)
elf_base = kernel_offset + payload_off
elf = memoryview(data)[elf_base:]
e_phoff = u64(elf, 0x20)
e_phnum = struct.unpack_from('<H', elf, 0x38)[0]

segs = []
for i in range(e_phnum):
    b = e_phoff + i * 56
    if u32(elf, b) == 1:
        vaddr    = u64(elf, b + 16)
        foff_elf = u64(elf, b + 8)
        fsz      = u64(elf, b + 32)
        memsz    = u64(elf, b + 40)
        flags    = u32(elf, b + 4)
        segs.append((vaddr, elf_base + foff_elf, fsz, memsz, flags))

def vaddr_to_foff(va):
    for (vaddr, foff_z, fsz, memsz, flags) in segs:
        if vaddr <= va < vaddr + fsz:
            return foff_z + (va - vaddr)
    return None

# text segment (첫 번째 exec segment)
text_seg = next(s for s in segs if s[4] & 1)
text_vaddr, text_foff_z, text_fsz = text_seg[0], text_seg[1], text_seg[2]
print("text: vaddr=0x%016x  foff_z=0x%x" % (text_vaddr, text_foff_z))

# --- 트램폴린 여유공간 탐색 ---
TRAMP_VADDR = 0xffffffff817956b5
TRAMP_FOFF  = text_foff_z + (TRAMP_VADDR - text_vaddr)

def alloc_tramp(size, hint=0):
    """hint 이후에서 size+16 바이트 0xCC 연속 블록 찾기"""
    for i in range(hint, 426 * 1024 - size - 16):
        if all(b == 0xCC for b in data[TRAMP_FOFF+i:TRAMP_FOFF+i+size+4]):
            return i
    raise RuntimeError("trampoline area full")

def serial_write_bytes(char):
    """COM1(0x3F8)에 char 1바이트를 쓰는 코드 (rdx, rax를 push/pop으로 보호)"""
    return (
        b'\x52'                          # push rdx
        b'\x50'                          # push rax
        b'\xba\xf8\x03\x00\x00'          # mov edx, 0x3F8
        + bytes([0xb0, ord(char)])        # mov al, char
        + b'\xee'                         # out dx, al
        + b'\x58'                         # pop rax
        + b'\x5a'                         # pop rdx
    )  # 12 bytes

# ────────────────────────────────────────────────
# 1.  startup_64 +0x77 probe ('K') — 이미 v1에서 삽입
#     입력 파일이 이미 probe 삽입된 상태라면 재삽입하지 않음.
# ────────────────────────────────────────────────
STARTUP64_REL  = 0x77
startup64_foff = text_foff_z + STARTUP64_REL
startup64_vaddr= text_vaddr  + STARTUP64_REL

if data[startup64_foff] == 0xE8:
    print("startup_64 probe (K) already present — skipping")
else:
    lgdt_bytes = bytes(data[startup64_foff:startup64_foff+7])
    assert lgdt_bytes[:3] == b'\x0f\x01\x15', "lgdt not found at startup_64+0x77"
    disp = struct.unpack_from('<i', lgdt_bytes, 3)[0]
    lgdt_target = (startup64_vaddr + 7 + disp) & 0xFFFFFFFFFFFFFFFF

    off = alloc_tramp(32)
    probe_vaddr = TRAMP_VADDR + off
    probe_foff  = TRAMP_FOFF  + off

    stub = bytearray()
    stub += serial_write_bytes('K')
    stub += b'\x48\xb8' + struct.pack('<Q', lgdt_target)  # mov rax, lgdt_target
    stub += b'\x0f\x01\x10'                               # lgdt [rax]
    stub += b'\xc3'                                       # ret

    data[probe_foff:probe_foff+len(stub)] = stub
    rel32 = struct.unpack('<i', struct.pack('<I',
        (probe_vaddr - (startup64_vaddr + 5)) & 0xFFFFFFFF))[0]
    data[startup64_foff:startup64_foff+7] = bytearray([0xE8]) + struct.pack('<i', rel32) + b'\x90\x90'
    print("K probe inserted at startup_64+0x77 → 0x%016x" % probe_vaddr)

# ────────────────────────────────────────────────
# 2.  x86_64_start_kernel probe ('X')
#     함수 시작: 53 48 89 fb 0f 20 e0 65 ...  (push rbx 로 시작, E8 아님)
# ────────────────────────────────────────────────
XSK_VADDR = 0xffffffff82a1a3bb
xsk_foff  = vaddr_to_foff(XSK_VADDR)
assert xsk_foff, "x86_64_start_kernel foff not found"
assert data[xsk_foff] == 0x53, "x86_64_start_kernel: expected push rbx (0x53), got 0x%02x" % data[xsk_foff]

# 첫 7바이트: 53(1) + 48 89 fb(3) + 0f 20 e0(3) = 완전한 명령어 3개
# stub 안에 push rbx 포함 → CALL/RET 방식은 스택 불균형
# → JMP rel32(E9) 방식 사용: stub 끝에서 XSK+7로 JMP back
orig7_xsk = bytes(data[xsk_foff:xsk_foff+7])  # 53 48 89 fb 0f 20 e0

off = alloc_tramp(48)
xsk_probe_vaddr = TRAMP_VADDR + off
xsk_probe_foff  = TRAMP_FOFF  + off

stub = bytearray()
stub += serial_write_bytes('X')
stub += orig7_xsk          # push rbx; mov rbx,rdi; mov rax,cr4 (스택 불균형 발생 안 함, JMP 복귀)
# JMP back to XSK+7 (orig 7바이트 이후)
jmp_back_from = xsk_probe_vaddr + len(stub) + 5
jmp_back_to   = XSK_VADDR + 7
jmp_rel = struct.unpack('<i', struct.pack('<I',
    (jmp_back_to - jmp_back_from) & 0xFFFFFFFF))[0]
stub += bytearray([0xE9]) + struct.pack('<i', jmp_rel)  # JMP XSK+7

data[xsk_probe_foff:xsk_probe_foff+len(stub)] = stub

# JMP rel32(5) + NOP NOP(2) = 7바이트로 원래 7바이트 덮어쓰기
jmp_rel32 = struct.unpack('<i', struct.pack('<I',
    (xsk_probe_vaddr - (XSK_VADDR + 5)) & 0xFFFFFFFF))[0]
data[xsk_foff:xsk_foff+7] = bytearray([0xE9]) + struct.pack('<i', jmp_rel32) + b'\x90\x90'
print("X probe inserted at x86_64_start_kernel → 0x%016x (JMP, 7-byte)" % xsk_probe_vaddr)

# ────────────────────────────────────────────────
# 3.  start_kernel probe ('S')
#     이미 E8(CALL) 트램폴린으로 시작. rel32를 새 stub으로 교체.
#     새 stub: serial 'S' → CALL 기존_트램폴린 → RET
# ────────────────────────────────────────────────
SK_VADDR = 0xffffffff82a1aa22
sk_foff  = vaddr_to_foff(SK_VADDR)
assert sk_foff, "start_kernel foff not found"
assert data[sk_foff] == 0xE8, "start_kernel: expected CALL (0xE8), got 0x%02x" % data[sk_foff]

old_rel32_sk = struct.unpack_from('<i', data, sk_foff+1)[0]
old_target_sk = (SK_VADDR + 5 + old_rel32_sk) & 0xFFFFFFFFFFFFFFFF

off = alloc_tramp(32)
sk_probe_vaddr = TRAMP_VADDR + off
sk_probe_foff  = TRAMP_FOFF  + off

stub = bytearray()
stub += serial_write_bytes('S')
# CALL old_target_sk
call_rel = struct.unpack('<i', struct.pack('<I',
    (old_target_sk - (sk_probe_vaddr + len(stub) + 5)) & 0xFFFFFFFF))[0]
stub += bytearray([0xE8]) + struct.pack('<i', call_rel)
stub += b'\xc3'

data[sk_probe_foff:sk_probe_foff+len(stub)] = stub
new_rel32_sk = struct.unpack('<i', struct.pack('<I',
    (sk_probe_vaddr - (SK_VADDR + 5)) & 0xFFFFFFFF))[0]
data[sk_foff+1:sk_foff+5] = struct.pack('<i', new_rel32_sk)
print("S probe inserted at start_kernel → 0x%016x  (old tramp→0x%016x)" % (
    sk_probe_vaddr, old_target_sk))

# ────────────────────────────────────────────────
# 4.  setup_arch probe ('A')
#     동일하게 E8(CALL) 트램폴린 래핑
# ────────────────────────────────────────────────
SA_VADDR = 0xffffffff82a21afc
sa_foff  = vaddr_to_foff(SA_VADDR)
assert sa_foff, "setup_arch foff not found"
assert data[sa_foff] == 0xE8, "setup_arch: expected CALL (0xE8), got 0x%02x" % data[sa_foff]

old_rel32_sa = struct.unpack_from('<i', data, sa_foff+1)[0]
old_target_sa = (SA_VADDR + 5 + old_rel32_sa) & 0xFFFFFFFFFFFFFFFF

off = alloc_tramp(32)
sa_probe_vaddr = TRAMP_VADDR + off
sa_probe_foff  = TRAMP_FOFF  + off

stub = bytearray()
stub += serial_write_bytes('A')
call_rel = struct.unpack('<i', struct.pack('<I',
    (old_target_sa - (sa_probe_vaddr + len(stub) + 5)) & 0xFFFFFFFF))[0]
stub += bytearray([0xE8]) + struct.pack('<i', call_rel)
stub += b'\xc3'

data[sa_probe_foff:sa_probe_foff+len(stub)] = stub
new_rel32_sa = struct.unpack('<i', struct.pack('<I',
    (sa_probe_vaddr - (SA_VADDR + 5)) & 0xFFFFFFFF))[0]
data[sa_foff+1:sa_foff+5] = struct.pack('<i', new_rel32_sa)
print("A probe inserted at setup_arch → 0x%016x  (old tramp→0x%016x)" % (
    sa_probe_vaddr, old_target_sa))

# ────────────────────────────────────────────────
# 5.  x86_64_start_kernel 내부 7개 CALL 직전 probe ('1'~'7')
#
#     x86_64_start_kernel = 0xffffffff82a1a3bb
#     CALL 목록 (원본 분석):
#       +0x00f → reset_early_page_tables   (0xffffffff82a1a15a)
#       +0x037 → clear_page_orig           (0xffffffff8144e460)
#       +0x03c → idt_setup_early_handler   (0xffffffff82a218ac)
#       +0x04d → copy_bootdata             (0xffffffff82a1a199)
#       +0x069 → __fentry__ (첫 번째)      (0xffffffff81049890)
#       +0x0e6 → 의심 CALL                 (0xffffffff882aeca8)
#       +0x10d → 의심 CALL2                (0xffffffff849a27d0)
#
#     각 CALL의 E8 rel32를 probe stub으로 교체.
#     stub: serial write → CALL 원래 target → RET
# ────────────────────────────────────────────────
XSK_CALLS = [
    (0x00f, '1', "reset_early_page_tables"),
    (0x037, '2', "clear_page_orig"),
    (0x03c, '3', "idt_setup_early_handler"),
    (0x04d, '4', "copy_bootdata"),
    (0x069, '5', "__fentry__(1st)"),
    (0x0e6, '6', "unknown_0xe6"),
    (0x10d, '7', "unknown_0x10d"),
]
XSK_VADDR2 = 0xffffffff82a1a3bb

for rel_off, char, label in XSK_CALLS:
    call_va   = XSK_VADDR2 + rel_off
    call_foff = vaddr_to_foff(call_va)
    assert call_foff, "XSK inner call %s foff not found" % label
    assert data[call_foff] == 0xE8, \
        "XSK +0x%03x (%s): expected E8, got 0x%02x" % (rel_off, label, data[call_foff])

    old_rel = struct.unpack_from('<i', data, call_foff+1)[0]
    old_tgt  = (call_va + 5 + old_rel) & 0xFFFFFFFFFFFFFFFF

    off = alloc_tramp(32)
    p_vaddr = TRAMP_VADDR + off
    p_foff  = TRAMP_FOFF  + off

    stub = bytearray()
    stub += serial_write_bytes(char)
    call_rel = struct.unpack('<i', struct.pack('<I',
        (old_tgt - (p_vaddr + len(stub) + 5)) & 0xFFFFFFFF))[0]
    stub += bytearray([0xE8]) + struct.pack('<i', call_rel)
    stub += b'\xc3'

    data[p_foff:p_foff+len(stub)] = stub
    new_rel = struct.unpack('<i', struct.pack('<I',
        (p_vaddr - (call_va + 5)) & 0xFFFFFFFF))[0]
    data[call_foff+1:call_foff+5] = struct.pack('<i', new_rel)
    print("'%s' probe @ XSK+0x%03x (%s) → stub=0x%016x  orig_tgt=0x%016x" % (
        char, rel_off, label, p_vaddr, old_tgt))

# ────────────────────────────────────────────────
# 6.  copy_bootdata 내부 7개 CALL 직전 probe ('a'~'g')
#
#     copy_bootdata = 0xffffffff82a1a199
#     CALL 목록:
#       +0x011 → sanitize_boot_params  (0xffffffff81756374)
#       +0x0f2 → reset_early_page_tables (0xffffffff82a1a15a)
#       +0x135 → unknown_c             (0xffffffff9299c7ee)
#       +0x1ab → unknown_d             (0xffffffff6322e45e)
#       +0x1f2 → unknown_e             (0xffffffff82a1a1e2)
#       +0x213 → copy_bootdata(self)   (0xffffffff82a1a199)
#       +0x218 → unknown_g             (0xffffffff82a1a481)
# ────────────────────────────────────────────────
CBD_VADDR = 0xffffffff82a1a199
CBD_CALLS = [
    (0x011, 'a', "sanitize_boot_params"),
    (0x0f2, 'b', "reset_early_page_tables"),
    (0x135, 'c', "unknown_c"),
    (0x1ab, 'd', "unknown_d"),
    (0x1f2, 'e', "unknown_e"),
    (0x213, 'f', "copy_bootdata_self"),
    (0x218, 'g', "unknown_g"),
]

for rel_off, char, label in CBD_CALLS:
    call_va   = CBD_VADDR + rel_off
    call_foff = vaddr_to_foff(call_va)
    assert call_foff, "CBD inner call %s foff not found" % label
    assert data[call_foff] == 0xE8, \
        "CBD +0x%03x (%s): expected E8, got 0x%02x" % (rel_off, label, data[call_foff])

    old_rel = struct.unpack_from('<i', data, call_foff+1)[0]
    old_tgt  = (call_va + 5 + old_rel) & 0xFFFFFFFFFFFFFFFF

    off = alloc_tramp(32)
    p_vaddr = TRAMP_VADDR + off
    p_foff  = TRAMP_FOFF  + off

    stub = bytearray()
    stub += serial_write_bytes(char)
    call_rel = struct.unpack('<i', struct.pack('<I',
        (old_tgt - (p_vaddr + len(stub) + 5)) & 0xFFFFFFFF))[0]
    stub += bytearray([0xE8]) + struct.pack('<i', call_rel)
    stub += b'\xc3'

    data[p_foff:p_foff+len(stub)] = stub
    new_rel = struct.unpack('<i', struct.pack('<I',
        (p_vaddr - (call_va + 5)) & 0xFFFFFFFF))[0]
    data[call_foff+1:call_foff+5] = struct.pack('<i', new_rel)
    print("'%s' probe @ CBD+0x%03x (%s) → stub=0x%016x  orig_tgt=0x%016x" % (
        char, rel_off, label, p_vaddr, old_tgt))

# ────────────────────────────────────────────────
# 7.  __early_make_pgtable 내부 CALL probe ('h'~'m')
#
#     __early_make_pgtable = 0xffffffff82a1a1e2
#     CALL 목록:
#       +0x0a9 → reset_early_page_tables (0xffffffff82a1a15a)
#       +0x0ec → unknown_h               (0xffffffff9299c7ee)
#       +0x162 → unknown_i               (0xffffffff6322e45e)
#       +0x1a9 → __early_make_pgtable    (0xffffffff82a1a1e2) 재귀
#       +0x1ca → copy_bootdata           (0xffffffff82a1a199)
#       +0x1cf → x86_early_init_platform_quirks (0xffffffff82a1a481)
# ────────────────────────────────────────────────
EMPT_VADDR = 0xffffffff82a1a1e2
EMPT_CALLS = [
    (0x0a9, 'h', "reset_early_page_tables"),
    (0x0ec, 'i', "unknown_h(9299c7ee)"),
    (0x162, 'j', "unknown_i(6322e45e)"),
    (0x1a9, 'k', "early_make_pgtable_recurse"),
    (0x1ca, 'l', "copy_bootdata"),
    (0x1cf, 'm', "x86_early_init_platform_quirks"),
]

for rel_off, char, label in EMPT_CALLS:
    call_va   = EMPT_VADDR + rel_off
    call_foff = vaddr_to_foff(call_va)
    assert call_foff, "EMPT inner call %s foff not found" % label
    assert data[call_foff] == 0xE8, \
        "EMPT +0x%03x (%s): expected E8, got 0x%02x" % (rel_off, label, data[call_foff])

    old_rel = struct.unpack_from('<i', data, call_foff+1)[0]
    old_tgt  = (call_va + 5 + old_rel) & 0xFFFFFFFFFFFFFFFF

    off = alloc_tramp(32)
    p_vaddr = TRAMP_VADDR + off
    p_foff  = TRAMP_FOFF  + off

    stub = bytearray()
    stub += serial_write_bytes(char)
    call_rel = struct.unpack('<i', struct.pack('<I',
        (old_tgt - (p_vaddr + len(stub) + 5)) & 0xFFFFFFFF))[0]
    stub += bytearray([0xE8]) + struct.pack('<i', call_rel)
    stub += b'\xc3'

    data[p_foff:p_foff+len(stub)] = stub
    new_rel = struct.unpack('<i', struct.pack('<I',
        (p_vaddr - (call_va + 5)) & 0xFFFFFFFF))[0]
    data[call_foff+1:call_foff+5] = struct.pack('<i', new_rel)
    print("'%s' probe @ EMPT+0x%03x (%s) → stub=0x%016x  orig=0x%016x" % (
        char, rel_off, label, p_vaddr, old_tgt))

# --- 저장 ---
save_gz(OUT, data)
md5 = hashlib.md5(data).hexdigest()
print("\n=== DONE ===")
print("Output: %s" % OUT)
print("MD5   : %s" % md5)
print("Expected serial: K X [1~7] S A  (각 단계 도달 시)")
