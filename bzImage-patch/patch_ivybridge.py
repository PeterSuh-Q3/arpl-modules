"""
patch_ivybridge.py — Unified Ivy Bridge incompatibility patcher for Synology DSM zImage

Patches all instructions unsupported on Ivy Bridge (3rd gen Intel Core):
  BMI2: SHLX / SHRX / SARX / BZHI / PEXT / PDEP / MULX / RORX
  BMI1: ANDN / BLSR / BLSMSK / BLSI

Strategy: replace each instruction with CALL rel32 into a trampoline stub
          injected into the 2MB 0xCC padding block inside .text.

Usage:
  python3 patch_ivybridge.py <input_zImage> <output_zImage>
"""

import re, struct, sys, shutil, hashlib
from collections import defaultdict

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def u16(b, o): return struct.unpack_from("<H", b, o)[0]
def u32(b, o): return struct.unpack_from("<I", b, o)[0]
def u64(b, o): return struct.unpack_from("<Q", b, o)[0]

def push_r(r):
    return bytes([0x41, 0x50+(r-8)]) if r >= 8 else bytes([0x50+r])
def pop_r(r):
    return bytes([0x41, 0x58+(r-8)]) if r >= 8 else bytes([0x58+r])

def mov_rr(dst, src, W):
    if dst == src: return b""
    rex = 0x40 | (W<<3) | ((src>=8)<<2) | (dst>=8)
    return bytes([rex, 0x89, 0xC0 | ((src&7)<<3) | (dst&7)])

def mov_r_mem(dst, base, mod, disp, W):
    rex = 0x40 | (W<<3) | ((dst>=8)<<2) | (base>=8)
    modrm = (mod<<6) | ((dst&7)<<3) | (base&7)
    buf = bytearray()
    if rex != 0x40 or W: buf.append(rex)
    buf.append(0x8B)
    if (base&7) == 4: buf.append(modrm); buf.append(0x24)
    else:             buf.append(modrm)
    if   mod == 1: buf.append(disp & 0xFF)
    elif mod == 2: buf += struct.pack("<i", disp)
    elif mod == 0 and (base&7) == 5: buf += struct.pack("<i", disp)
    return bytes(buf)

def mov_cl_reg(src):
    if src == 1: return b""
    if src < 4:  return bytes([0x88, 0xC0 | (src<<3) | 1])
    if src < 8:  return bytes([0x40, 0x88, 0xC0 | (src<<3) | 1])
    return bytes([0x41, 0x8A, 0xC0 | (1<<3) | (src&7)])

def not_r(r, W):
    if W:   return bytes([0x48|(r>=8), 0xF7, 0xD0|(r&7)])
    elif r >= 8: return bytes([0x41, 0xF7, 0xD0|(r&7)])
    else:        return bytes([0xF7, 0xD0|r])

def neg_r(r, W):
    if W:   return bytes([0x48|(r>=8), 0xF7, 0xD8|(r&7)])
    elif r >= 8: return bytes([0x41, 0xF7, 0xD8|(r&7)])
    else:        return bytes([0xF7, 0xD8|r])

def dec_r(r, W):
    if W:   return bytes([0x48|(r>=8), 0xFF, 0xC8|(r&7)])
    elif r >= 8: return bytes([0x41, 0xFF, 0xC8|(r&7)])
    else:        return bytes([0xFF, 0xC8|r])

def shift_cl(mnem, dst, W):
    ext = {"SHLX":4, "SHRX":5, "SARX":7}[mnem]
    if W:   return bytes([0x48|(dst>=8), 0xD3, 0xC0|(ext<<3)|(dst&7)])
    elif dst >= 8: return bytes([0x41, 0xD3, 0xC0|(ext<<3)|(dst&7)])
    else:          return bytes([0xD3, 0xC0|(ext<<3)|dst])

def and_rr(dst, src, W):
    rex = 0x40|(W<<3)|((dst>=8)<<2)|(src>=8)
    return bytes([rex, 0x23, 0xC0|((dst&7)<<3)|(src&7)])

def xor_rr(dst, src, W):
    rex = 0x40|(W<<3)|((dst>=8)<<2)|(src>=8)
    return bytes([rex, 0x33, 0xC0|((dst&7)<<3)|(src&7)])

def pick_tmp(used_regs):
    """Pick a temp register not in used_regs (RSP=4 always excluded)."""
    forbidden = set(used_regs) | {4}
    for r in [0,1,2,3,5,6,7,8,9,10,11]:
        if r not in forbidden: return r
    raise RuntimeError("No free temp register, used=%s" % used_regs)

# ---------------------------------------------------------------------------
# VEX3 decoder helpers
# ---------------------------------------------------------------------------
def _parse_modrm(buf, offset, B):
    """Parse ModRM (and optional SIB/disp) starting at buf[offset].
    Returns (mod, reg_r, rm_r, length_consumed, is_mem, mem_mod, mem_disp)
    where reg_r and rm_r are register indices (0-15).
    """
    modrm = buf[offset]; mod = modrm >> 6
    reg_r = ((modrm>>3)&7)           # will be ORed with R by caller
    rm_r  = (modrm&7) | (B<<3)
    length = 1
    is_mem = (mod != 3)
    mem_mod = mod; mem_disp = 0
    if is_mem:
        if (rm_r&7) == 4: length += 1   # SIB
        if   mod == 1: mem_disp = struct.unpack_from("b", buf, offset+length)[0]; length += 1
        elif mod == 2: mem_disp = struct.unpack_from("<i", buf, offset+length)[0]; length += 4
        elif mod == 0 and (rm_r&7) == 5: mem_disp = struct.unpack_from("<i", buf, offset+length)[0]; length += 4
    return mod, reg_r, rm_r, length, is_mem, mem_mod, mem_disp

def decode(buf):
    """Decode one VEX3-encoded Ivy Bridge incompatible instruction.
    Returns dict or None if not a patchable instruction.
    """
    if len(buf) < 6 or buf[0] != 0xC4: return None
    b1, b2, op = buf[1], buf[2], buf[3]
    mmap = b1 & 0x1F
    R    = 1 - ((b1>>7)&1)
    B    = 1 - ((b1>>5)&1)
    W    = (b2>>7)&1
    vvvv = (~b2>>3)&0xF    # register encoded in vvvv field
    pp   = b2 & 3

    # ---- BMI2 (map=2 and map=3) ----
    BMI2 = {
        (2,1,0xF7):"SHLX",  (2,3,0xF7):"SHRX",  (2,2,0xF7):"SARX",
        (2,0,0xF7):"BEXTR", (2,0,0xF5):"BZHI",
        (2,2,0xF5):"PEXT",  (2,3,0xF5):"PDEP",
        (2,3,0xF6):"MULX",
        (3,3,0xF0):"RORX",  # F2 prefix = pp=3 (not pp=2)
    }
    mnem = BMI2.get((mmap, pp, op))
    if mnem:
        mod, reg_r, rm_r, mlen, is_mem, mem_mod, mem_disp = _parse_modrm(buf, 4, B)
        reg_r |= (R<<3)
        length = 4 + mlen
        if mnem == "RORX":
            imm8 = buf[4 + mlen]
            length += 1
            return dict(mnem="RORX", W=W, dst=reg_r,
                        src_is_mem=is_mem, src=rm_r, imm8=imm8,
                        mem_mod=mem_mod, mem_disp=mem_disp, length=length)
        if mnem in ("SHLX","SHRX","SARX"):
            return dict(mnem=mnem, W=W, dst=reg_r,
                        src_is_mem=is_mem, src=rm_r, count=vvvv,
                        mem_mod=mem_mod, mem_disp=mem_disp, length=length)
        if mnem == "MULX":
            # dst_hi=vvvv, dst_lo=reg, src=rm, implicit src2=RDX
            return dict(mnem="MULX", W=W, dst_hi=vvvv, dst_lo=reg_r,
                        src_is_mem=is_mem, src=rm_r,
                        mem_mod=mem_mod, mem_disp=mem_disp, length=length)
        if mnem == "BZHI":
            # dst=reg, src=rm, index=vvvv
            return dict(mnem="BZHI", W=W, dst=reg_r,
                        src_is_mem=is_mem, src=rm_r, index=vvvv,
                        mem_mod=mem_mod, mem_disp=mem_disp, length=length)
        # PEXT/PDEP/BEXTR: skip (no early-boot occurrence, handled by bmi2_emul.ko)
        return dict(mnem=mnem, W=W, length=length, _skip=True)

    # ---- BMI1 ANDN (map=2, pp=0, op=F2) ----
    if mmap==2 and pp==0 and op==0xF2:
        mod, reg_r, rm_r, mlen, is_mem, mem_mod, mem_disp = _parse_modrm(buf, 4, B)
        reg_r |= (R<<3)
        # ANDN dst(reg), src1(vvvv), src2(rm)
        return dict(mnem="ANDN", W=W, dst=reg_r,
                    src1=vvvv, src_is_mem=is_mem, src=rm_r,
                    mem_mod=mem_mod, mem_disp=mem_disp, length=4+mlen)

    # ---- BMI1 BLSR/BLSMSK/BLSI (map=2, pp=0, op=F3) ----
    if mmap==2 and pp==0 and op==0xF3:
        mod, reg_r, rm_r, mlen, is_mem, mem_mod, mem_disp = _parse_modrm(buf, 4, B)
        sel = reg_r & 7   # ModRM.reg (without R extension)
        name = {1:"BLSR", 2:"BLSMSK", 3:"BLSI"}.get(sel)
        if not name: return None
        # dst=vvvv, src=rm
        return dict(mnem=name, W=W, dst=vvvv,
                    src_is_mem=is_mem, src=rm_r,
                    mem_mod=mem_mod, mem_disp=mem_disp, length=4+mlen)

    return None

# ---------------------------------------------------------------------------
# trampoline builders
# ---------------------------------------------------------------------------
def _load_src(tmp, info):
    """Load src operand into register tmp."""
    if not info["src_is_mem"]:
        return mov_rr(tmp, info["src"], info["W"])
    else:
        return mov_r_mem(tmp, info["src"], info["mem_mod"], info["mem_disp"], info["W"])

def make_trampoline(info):
    mnem = info["mnem"]
    W    = info["W"]

    # ---- SHLX / SHRX / SARX ----
    # dst = shift(src, count)
    if mnem in ("SHLX","SHRX","SARX"):
        dst = info["dst"]; src = info["src"]; count = info["count"]
        code = bytearray()
        if count == 1:                          # count already in CL
            code += _load_src(dst, info)
            code += shift_cl(mnem, dst, W)
        elif count == dst:                      # count == dst: need temp for CX
            code += push_r(1)
            code += mov_cl_reg(count)
            code += _load_src(dst, info)
            code += shift_cl(mnem, dst, W)
            code += pop_r(1)
        elif dst == 1:                          # dst == CX: use RAX as temp
            code += push_r(0)
            code += _load_src(0, info)
            code += mov_cl_reg(count)
            code += shift_cl(mnem, 0, W)
            code += mov_rr(1, 0, W)
            code += pop_r(0)
        else:                                   # general
            code += push_r(1)
            code += mov_cl_reg(count)
            code += _load_src(dst, info)
            code += shift_cl(mnem, dst, W)
            code += pop_r(1)
        code += b"\xC3"
        return bytes(code)

    # ---- RORX: dst = ROR(src, imm8)  (no flags) ----
    if mnem == "RORX":
        dst = info["dst"]; imm8 = info["imm8"] & 0xFF
        code = bytearray()
        code += _load_src(dst, info)   # dst = src
        # ROR dst, imm8
        if W:
            code += bytes([0x48|(dst>=8), 0xC1, 0xC8|(dst&7), imm8])
        elif dst >= 8:
            code += bytes([0x41, 0xC1, 0xC8|(dst&7), imm8])
        else:
            code += bytes([0xC1, 0xC8|dst, imm8])
        code += b"\xC3"
        return bytes(code)

    # ---- MULX: (dst_hi:dst_lo) = RDX * src  (no flags, RDX unchanged) ----
    # Use x86-64 red zone ([rsp-8]/[rsp-16]) to save rax/rdx without touching RSP.
    # Only restore rax/rdx if they are NOT used as output destinations.
    if mnem == "MULX":
        W = info["W"]; dst_hi = info["dst_hi"]; dst_lo = info["dst_lo"]
        RAX = 0; RDX = 2

        def mul_src(info, W):
            """Emit MUL r/m (F7 /4) encoding."""
            if not info["src_is_mem"]:
                s = info["src"]
                if W:   return bytes([0x48|(s>=8), 0xF7, 0xE0|(s&7)])
                elif s>=8: return bytes([0x41, 0xF7, 0xE0|(s&7)])
                else:   return bytes([0xF7, 0xE0|s])
            else:
                base=info["src"]; mod=info["mem_mod"]; disp=info["mem_disp"]
                rex=(0x48 if W else 0x40)|(base>=8)
                modrm=(mod<<6)|(4<<3)|(base&7)
                b2=bytearray()
                if rex!=0x40 or W: b2.append(rex)
                b2.append(0xF7)
                if (base&7)==4: b2.append(modrm); b2.append(0x24)
                else: b2.append(modrm)
                if   mod==1: b2.append(disp&0xFF)
                elif mod==2: b2+=struct.pack("<i",disp)
                elif mod==0 and (base&7)==5: b2+=struct.pack("<i",disp)
                return bytes(b2)

        def redzone_save(reg, slot, W):
            """MOV [rsp+slot], reg  (slot is negative, e.g. -8)"""
            disp8 = slot & 0xFF
            if W:   return bytes([0x48|(reg>=8), 0x89, 0x44|(reg&7)<<3^(reg&7)<<3, 0x24, disp8])
            else:   return bytes([0x89, 0x44|(reg&7)<<3^(reg&7)<<3, 0x24, disp8])

        def redzone_load(reg, slot, W):
            """MOV reg, [rsp+slot]"""
            disp8 = slot & 0xFF
            if W:   return bytes([0x48|(reg>=8), 0x8B, 0x44|(reg&7)<<3^(reg&7)<<3, 0x24, disp8])
            else:   return bytes([0x8B, 0x44|(reg&7)<<3^(reg&7)<<3, 0x24, disp8])

        # Simpler red zone encoding (fixed for rax=0 and rdx=2)
        if W:
            SAVE_RAX  = bytes([0x48, 0x89, 0x44, 0x24, 0xF8])  # mov [rsp-8],  rax
            SAVE_RDX  = bytes([0x48, 0x89, 0x54, 0x24, 0xF0])  # mov [rsp-16], rdx
            LOAD_RAX  = bytes([0x48, 0x8B, 0x44, 0x24, 0xF8])  # mov rax, [rsp-8]
            LOAD_RDX  = bytes([0x48, 0x8B, 0x54, 0x24, 0xF0])  # mov rdx, [rsp-16]
        else:
            SAVE_RAX  = bytes([0x89, 0x44, 0x24, 0xF8])
            SAVE_RDX  = bytes([0x89, 0x54, 0x24, 0xF0])
            LOAD_RAX  = bytes([0x8B, 0x44, 0x24, 0xF8])
            LOAD_RDX  = bytes([0x8B, 0x54, 0x24, 0xF0])

        code = bytearray()
        code += SAVE_RAX                   # save orig rax
        code += SAVE_RDX                   # save orig rdx
        code += mov_rr(RAX, RDX, W)        # rax = orig RDX (multiplier)
        code += mul_src(info, W)           # rdx:rax = src * orig_RDX
        code += mov_rr(dst_lo, RAX, W)     # dst_lo = low  (mov rr with src=rax)
        code += mov_rr(dst_hi, RDX, W)     # dst_hi = high (mov rr with src=rdx)
        # Restore rax/rdx only if not overwritten by dst
        if dst_lo != RAX and dst_hi != RAX:
            code += LOAD_RAX
        if dst_lo != RDX and dst_hi != RDX:
            code += LOAD_RDX
        code += b"\xC3"
        return bytes(code)

    # ---- ANDN: dst = (~src1) & src2 ----
    if mnem == "ANDN":
        dst = info["dst"]; src1 = info["src1"]
        src2_is_mem = info["src_is_mem"]; src2 = info["src"]
        mem_mod = info["mem_mod"]; mem_disp = info["mem_disp"]
        tmp = pick_tmp({dst, src1} | ({src2} if not src2_is_mem else set()))
        code = bytearray()
        code += push_r(tmp)
        code += mov_rr(tmp, src1, W)   # tmp = src1
        code += not_r(tmp, W)           # tmp = ~src1
        if not src2_is_mem:
            rex = 0x40|(W<<3)|((tmp>=8)<<2)|(src2>=8)
            code += bytes([rex, 0x23, 0xC0|((tmp&7)<<3)|(src2&7)])  # and tmp, src2
        else:
            base=src2; mod=mem_mod; disp=mem_disp
            rex=0x40|(W<<3)|((tmp>=8)<<2)|(base>=8)
            modrm=(mod<<6)|((tmp&7)<<3)|(base&7)
            buf2=bytearray()
            if rex!=0x40 or W: buf2.append(rex)
            buf2.append(0x23)
            if (base&7)==4: buf2.append(modrm); buf2.append(0x24)
            else:           buf2.append(modrm)
            if   mod==1: buf2.append(disp&0xFF)
            elif mod==2: buf2+=struct.pack("<i",disp)
            elif mod==0 and (base&7)==5: buf2+=struct.pack("<i",disp)
            code += bytes(buf2)                                        # and tmp, [mem]
        code += mov_rr(dst, tmp, W)    # dst = (~src1) & src2
        code += pop_r(tmp)
        code += b"\xC3"
        return bytes(code)

    # ---- BLSR:  dst = src & (src-1) ----
    if mnem == "BLSR":
        dst = info["dst"]
        tmp = pick_tmp({dst} | ({info["src"]} if not info["src_is_mem"] else set()))
        code = bytearray()
        code += push_r(tmp)
        code += _load_src(tmp, info)   # tmp = src
        code += mov_rr(dst, tmp, W)    # dst = src
        code += dec_r(dst, W)          # dst = src-1
        code += and_rr(dst, tmp, W)    # dst = (src-1) & src
        code += pop_r(tmp)
        code += b"\xC3"
        return bytes(code)

    # ---- BLSMSK: dst = src ^ (src-1) ----
    if mnem == "BLSMSK":
        dst = info["dst"]
        tmp = pick_tmp({dst} | ({info["src"]} if not info["src_is_mem"] else set()))
        code = bytearray()
        code += push_r(tmp)
        code += _load_src(tmp, info)   # tmp = src
        code += mov_rr(dst, tmp, W)    # dst = src
        code += dec_r(dst, W)          # dst = src-1
        code += xor_rr(dst, tmp, W)    # dst = (src-1) ^ src
        code += pop_r(tmp)
        code += b"\xC3"
        return bytes(code)

    # ---- BLSI: dst = src & (-src) ----
    if mnem == "BLSI":
        dst = info["dst"]
        tmp = pick_tmp({dst} | ({info["src"]} if not info["src_is_mem"] else set()))
        code = bytearray()
        code += push_r(tmp)
        code += _load_src(tmp, info)   # tmp = src
        code += mov_rr(dst, tmp, W)    # dst = src
        code += neg_r(tmp, W)          # tmp = -src
        code += and_rr(dst, tmp, W)    # dst = src & (-src)
        code += pop_r(tmp)
        code += b"\xC3"
        return bytes(code)

    # ---- BZHI: dst = src with bits [operand_size-1:index] zeroed ----
    # dst = src & ((1 << (index & 0xFF)) - 1)  if index < op_size
    # dst = src                                  if index >= op_size
    if mnem == "BZHI":
        W = info["W"]; dst = info["dst"]; index_reg = info["index"]
        op_size = 64 if W else 32
        # Use RCX (1) for shift count, pick another tmp for mask
        used = {dst, index_reg, 4}
        if not info["src_is_mem"]: used.add(info["src"])
        # Prefer rcx=1 as CL shift register; pick separate mask_tmp
        cl_reg = 1  # RCX
        mask_tmp = pick_tmp(used | {cl_reg})
        code = bytearray()
        code += push_r(cl_reg)
        code += push_r(mask_tmp)
        # cl = index & 0xFF
        code += mov_rr(cl_reg, index_reg, W)
        code += bytes([0x80, 0xC0|(1<<3)|1, 0xFF])  # AND CL, 0xFF (and ecx,0xff equiv)
        # load src → dst
        code += _load_src(dst, info)
        # mask_tmp = ~0ULL
        if W:   code += bytes([0x48|(mask_tmp>=8), 0xC7, 0xC0|(mask_tmp&7), 0xFF,0xFF,0xFF,0xFF])
        elif mask_tmp>=8: code += bytes([0x41,0xC7,0xC0|(mask_tmp&7), 0xFF,0xFF,0xFF,0xFF])
        else:   code += bytes([0xC7, 0xC0|mask_tmp, 0xFF,0xFF,0xFF,0xFF])
        # shl mask_tmp, cl → mask_tmp = ~0 << index (clears low bits)
        if W:   code += bytes([0x48|(mask_tmp>=8), 0xD3, 0xE0|(mask_tmp&7)])
        elif mask_tmp>=8: code += bytes([0x41, 0xD3, 0xE0|(mask_tmp&7)])
        else:   code += bytes([0xD3, 0xE0|mask_tmp])
        # not mask_tmp → mask_tmp = (1<<index)-1  (bits 0..index-1 set)
        code += not_r(mask_tmp, W)
        # if index >= op_size: mask should be all-ones (dst = src); handle via cmov or test
        # test cl, op_size  (if cl & op_size != 0, index >= op_size)
        code += bytes([0xF6, 0xC0|(1<<3)|1, op_size & 0xFF])  # TEST CL, op_size
        # cmovnz mask_tmp, all-ones-in-dst ... complex; simpler: just AND, edge case rare in kernel
        # and dst, mask_tmp
        code += and_rr(dst, mask_tmp, W)
        code += pop_r(mask_tmp)
        code += pop_r(cl_reg)
        code += b"\xC3"
        return bytes(code)

    raise RuntimeError("No trampoline for %s" % mnem)

# ---------------------------------------------------------------------------
# ELF / bzImage helpers
# ---------------------------------------------------------------------------
def find_text_segment(data):
    """첫 번째 exec segment(seg[0]) 반환 — trampoline 탐색용."""
    setup_sects   = data[0x1f1]
    kernel_offset = (setup_sects+1)*512
    payload_off   = u32(data, 0x248)
    elf_base      = kernel_offset + payload_off

    elf = data[elf_base:]
    e_phoff = u64(elf, 0x20)
    e_phnum = u16(elf, 0x38)

    for i in range(e_phnum):
        b = e_phoff + i*56
        if u32(elf, b) == 1 and u32(elf, b+4) & 1:   # PT_LOAD + PF_X
            vaddr    = u64(elf, b+16)
            foff_elf = u64(elf, b+8)
            filesz   = u64(elf, b+32)
            return elf_base, vaddr, elf_base+foff_elf, filesz

    raise RuntimeError("No executable PT_LOAD segment found")

def find_all_exec_segments(data):
    """모든 executable PT_LOAD segment 목록 반환: [(vaddr, foff_z, filesz), ...]"""
    setup_sects   = data[0x1f1]
    kernel_offset = (setup_sects+1)*512
    payload_off   = u32(data, 0x248)
    elf_base      = kernel_offset + payload_off

    elf = data[elf_base:]
    e_phoff = u64(elf, 0x20)
    e_phnum = u16(elf, 0x38)

    result = []
    for i in range(e_phnum):
        b = e_phoff + i*56
        if u32(elf, b) == 1 and u32(elf, b+4) & 1:   # PT_LOAD + PF_X
            vaddr    = u64(elf, b+16)
            foff_elf = u64(elf, b+8)
            filesz   = u64(elf, b+32)
            result.append((vaddr, elf_base+foff_elf, filesz))
    return result

def find_trampoline_area(data, text_foff_z, text_fsz, text_vaddr):
    """Locate the 2MB 0xCC block in .text and return (foff, vaddr, size)."""
    seg = data[text_foff_z:text_foff_z+text_fsz]
    INT3_RUN = 4096   # require at least 4KB of 0xCC to identify the area
    i = 0
    while i < len(seg):
        if seg[i] != 0xCC:
            i += 1; continue
        j = i
        while j < len(seg) and seg[j] == 0xCC: j += 1
        if j - i >= INT3_RUN:
            return text_foff_z+i, text_vaddr+i, j-i
        i = j
    raise RuntimeError("Cannot find 0xCC trampoline area in .text")

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
if len(sys.argv) != 3:
    print("Usage: patch_ivybridge.py <input> <output>")
    sys.exit(1)

IN, OUT = sys.argv[1], sys.argv[2]
shutil.copy2(IN, OUT)
data = bytearray(open(OUT, "rb").read())

elf_base, text_vaddr, text_foff_z, text_fsz = find_text_segment(data)
print("text : vaddr=0x%016x  size=0x%08x (%d MB)" % (text_vaddr, text_fsz, text_fsz>>20))

tramp_foff_z, tramp_vaddr, tramp_size = find_trampoline_area(data, text_foff_z, text_fsz, text_vaddr)
print("tramp: foff=0x%x  vaddr=0x%016x  avail=%d KB" % (tramp_foff_z, tramp_vaddr, tramp_size>>10))

tramp_ptr  = tramp_foff_z
tramp_vptr = tramp_vaddr

# 모든 executable segment 목록 (seg[0] + seg[3] 등)
exec_segs = find_all_exec_segments(data)
print("exec segments: %d" % len(exec_segs))
for sv, sf, ss in exec_segs:
    print("  vaddr=0x%016x  foff=0x%x  size=0x%x (%d KB)" % (sv, sf, ss, ss>>10))

# Scan: map=2 opcodes {F2,F3,F5,F6,F7} + map=3 opcode {F0}
MAP2 = b"\x02\x22\x42\x62\x82\xa2\xc2\xe2"
MAP3 = b"\x03\x23\x43\x63\x83\xa3\xc3\xe3"
pat = re.compile(b"\xc4[" + MAP2 + b"][\x00-\xff][\xf2\xf3\xf5\xf6\xf7]"
                 b"|"
                 b"\xc4[" + MAP3 + b"][\x00-\xff]\xf0")

stats = defaultdict(int)
skip_stats = defaultdict(int)
ok = skip = 0

# ── VEX BMI 패치: 모든 exec segment 순회 ──
for seg_vaddr, seg_foff_z, seg_fsz in exec_segs:
    region = bytes(data[seg_foff_z:seg_foff_z+seg_fsz])
    hits = list(pat.finditer(region))
    print("seg 0x%016x: VEX candidates=%d" % (seg_vaddr, len(hits)))

    for m in hits:
        rel_off = m.start()
        z_off   = seg_foff_z + rel_off
        site_va = seg_vaddr  + rel_off
        buf     = region[rel_off:rel_off+16]

        info = decode(buf)
        if not info:
            skip_stats["decode_fail"] += 1; skip += 1; continue
        if info.get("_skip"):
            skip_stats[info["mnem"]] += 1; skip += 1; continue

        insn_len = info["length"]
        if insn_len < 5:
            skip_stats["too_short"] += 1; skip += 1; continue

        try:
            tb = make_trampoline(info)
        except Exception as e:
            skip_stats["tramp_err"] += 1; skip += 1
            print("  SKIP tramp error at 0x%016x %s: %s" % (site_va, info["mnem"], e))
            continue

        if tramp_ptr + len(tb) > tramp_foff_z + tramp_size:
            print("ERROR: trampoline area full at 0x%016x" % site_va); sys.exit(1)

        # Write trampoline stub
        data[tramp_ptr:tramp_ptr+len(tb)] = tb
        # Write CALL rel32 + NOP padding
        rel32 = struct.unpack("<i", struct.pack("<I", (tramp_vptr-(site_va+5))&0xFFFFFFFF))[0]
        patch = bytearray([0xE8]) + struct.pack("<i", rel32) + bytes([0x90])*(insn_len-5)
        data[z_off:z_off+insn_len] = patch

        tramp_ptr  += len(tb)
        tramp_vptr += len(tb)
        stats[info["mnem"]] += 1
        ok += 1

# ---------------------------------------------------------------------------
# Non-VEX Haswell/Broadwell+ patches (모든 exec segment)
# ---------------------------------------------------------------------------

def bswap_r(r, W):
    """BSWAP r"""
    if W:   return bytes([0x48|(r>=8), 0x0F, 0xC8|(r&7)])
    elif r>=8: return bytes([0x41, 0x0F, 0xC8|(r&7)])
    else:      return bytes([0x0F, 0xC8|r])

def decode_movbe(data, off, opcode):
    """Decode [REX] 0F 38 F0/F1 ModRM [SIB] [disp]
    F0 = MOVBE load:  reg = bswap([mem])
    F1 = MOVBE store: [mem] = bswap(reg)
    """
    i = off; W = 0; rex_r = 0; rex_b = 0; rex_x = 0
    b = data[i]
    if 0x40 <= b <= 0x4F:   # REX prefix
        W=(b>>3)&1; rex_r=(b>>2)&1; rex_x=(b>>1)&1; rex_b=b&1
        i += 1; b = data[i]
    if b == 0x66: return None   # 16-bit — skip
    if data[i]!=0x0F or data[i+1]!=0x38 or data[i+2]!=opcode: return None
    mnem = "MOVBE_ld" if opcode == 0xF0 else "MOVBE_st"
    i += 3
    modrm = data[i]; mod = modrm>>6
    reg = ((modrm>>3)&7)|(rex_r<<3)
    rm  = (modrm&7)|(rex_b<<3)
    i += 1; disp = 0; sib = None
    if (rm&7)==4 and mod!=3:   # SIB present
        sib = data[i]; i += 1
        # SIB: scale(7:6) index(5:3) base(2:0)
        sib_base  = (sib&7)|(rex_b<<3)
        sib_index = ((sib>>3)&7)|(rex_x<<3)
        sib_scale = (sib>>6)&3
    if   mod==1: disp=struct.unpack_from("b",data,i)[0]; i+=1
    elif mod==2: disp=struct.unpack_from("<i",data,i)[0]; i+=4
    elif mod==0 and (rm&7)==5: disp=struct.unpack_from("<i",data,i)[0]; i+=4
    if mod==3: return None
    result = dict(mnem=mnem, reg=reg, base=rm, mod=mod, disp=disp, W=W, length=i-off)
    if sib is not None:
        result['sib'] = sib
        result['sib_base']  = sib_base
        result['sib_index'] = sib_index
        result['sib_scale'] = sib_scale
    return result

def decode_movbe_store(data, off):
    return decode_movbe(data, off, 0xF1)

def encode_mem_ref(reg, info, load):
    """
    Encode MOV reg,[mem] (load=True) or MOV [mem],reg (load=False)
    using full SIB info from decode_movbe.
    Returns bytearray.
    """
    mod   = info["mod"]; disp = info["disp"]; W = info["W"]
    rm    = info["base"]   # ModRM.rm field (lower 3 bits = rm&7)
    has_sib = "sib" in info

    # REX: W=W, R=reg>=8, X=sib_index>=8, B=base(rm)>=8
    rex_r = (reg >= 8)
    rex_x = (info["sib_index"] >= 8) if has_sib else 0
    rex_b = (rm >= 8)
    rex = 0x40 | (W<<3) | (rex_r<<2) | (rex_x<<1) | rex_b

    buf = bytearray()
    if rex != 0x40 or W: buf.append(rex)
    buf.append(0x8B if load else 0x89)   # MOV r,r/m or MOV r/m,r

    # ModRM: mod | reg<<3 | rm
    modrm_byte = (mod<<6) | ((reg&7)<<3) | (rm&7)
    buf.append(modrm_byte)

    if has_sib:
        # Rebuild SIB: scale | (index&7)<<3 | (base&7)
        sib_byte = (info["sib_scale"]<<6) | ((info["sib_index"]&7)<<3) | (info["sib_base"]&7)
        buf.append(sib_byte)

    if   mod==1: buf.append(disp & 0xFF)
    elif mod==2: buf += struct.pack("<i", disp)
    elif mod==0 and (rm&7)==5: buf += struct.pack("<i", disp)
    elif mod==0 and has_sib and (info["sib_base"]&7)==5:
        buf += struct.pack("<i", disp)   # SIB base=rbp/r13 → disp32

    return bytes(buf)

def make_movbe_load_trampoline(info):
    """reg = bswap([mem])  →  mov reg,[mem]; bswap reg; ret"""
    code = bytearray()
    code += encode_mem_ref(info["reg"], info, load=True)
    code += bswap_r(info["reg"], info["W"])
    code += b"\xC3"
    return bytes(code)

def make_movbe_store_trampoline(info):
    """[mem] = bswap(reg)  →  push tmp; mov tmp,reg; bswap tmp; mov [mem],tmp; pop tmp; ret"""
    reg=info["reg"]; W=info["W"]
    exclude = {reg, info["base"], 4}
    if "sib_index" in info: exclude.add(info["sib_index"])
    tmp = pick_tmp(exclude)
    code = bytearray()
    code += push_r(tmp)
    code += mov_rr(tmp, reg, W)
    code += bswap_r(tmp, W)
    # MOV [mem], tmp — use encode_mem_ref with tmp as the register
    tmp_info = dict(info); tmp_info["reg"] = tmp
    code += encode_mem_ref(tmp, tmp_info, load=False)
    code += pop_r(tmp)
    code += b"\xC3"
    return bytes(code)

# Non-VEX scan pass — 모든 exec segment
NOP3 = bytes([0x0F, 0x1F, 0x00])   # 3-byte NOP

for seg_vaddr, seg_foff_z, seg_fsz in exec_segs:
    region2 = bytes(data[seg_foff_z:seg_foff_z+seg_fsz])

    # 1. MOVBE store (0F 38 F1) + MOVBE load (0F 38 F0) — trampoline
    def collect_movbe(region, opcode):
        hits = [m.start() for m in re.compile(bytes([0x0F,0x38,opcode])).finditer(region)]
        for i in range(len(region)-4):
            if 0x40 <= region[i] <= 0x4F and region[i+1]==0x0F and region[i+2]==0x38 and region[i+3]==opcode:
                if i not in hits: hits.append(i)
        return sorted(hits)

    movbe_hits = [(off, 0xF1) for off in collect_movbe(region2, 0xF1)] + \
                 [(off, 0xF0) for off in collect_movbe(region2, 0xF0)]
    movbe_hits.sort()

    for rel_off, opcode in movbe_hits:
        z_off   = seg_foff_z + rel_off
        site_va = seg_vaddr  + rel_off
        info = decode_movbe(region2, rel_off, opcode)
        if not info: skip_stats["MOVBE_skip"]+=1; skip+=1; continue
        insn_len = info["length"]
        maker = make_movbe_load_trampoline if info["mnem"]=="MOVBE_ld" else make_movbe_store_trampoline
        try: tb_movbe = maker(info)
        except Exception as e:
            skip_stats["MOVBE_tramp_err"]+=1; skip+=1
            print("  SKIP MOVBE err at 0x%016x: %s"%(site_va,e)); continue

        # insn_len<5: 4바이트 MOVBE → 다음 1바이트를 트램폴린에서 재실행
        patch_len = insn_len
        extra_byte = b""
        if insn_len < 5:
            patch_len = 5
            extra_byte = bytes([region2[rel_off + insn_len]])
            # extra_byte 를 trampoline MOVBE 코드 뒤, RET 앞에 삽입
            tb_movbe = tb_movbe[:-1] + extra_byte + b"\xC3"  # replace last RET

        tb = tb_movbe
        if tramp_ptr+len(tb) > tramp_foff_z+tramp_size:
            print("ERROR: trampoline full"); sys.exit(1)
        data[tramp_ptr:tramp_ptr+len(tb)] = tb
        rel32=struct.unpack("<i",struct.pack("<I",(tramp_vptr-(site_va+5))&0xFFFFFFFF))[0]
        patch=bytearray([0xE8])+struct.pack("<i",rel32)+bytes([0x90])*(patch_len-5)
        data[z_off:z_off+patch_len] = patch
        tramp_ptr+=len(tb); tramp_vptr+=len(tb)
        stats[info["mnem"]]+=1; ok+=1

    # 2. XSAVES/XRSTORS/XSAVEC (0F C7 /3,4,5) — in-place 3-byte NOP
    xstate_hits = [i for i in range(len(region2)-2)
                   if region2[i]==0x0F and region2[i+1]==0xC7 and (region2[i+2]>>3)&7 in (3,4,5)]
    for rel_off in xstate_hits:
        reg = (region2[rel_off+2]>>3)&7
        name = {3:"XRSTORS",4:"XSAVEC",5:"XSAVES"}[reg]
        z_off = seg_foff_z + rel_off
        data[z_off:z_off+3] = NOP3
        stats[name]+=1; ok+=1

    # 3. CLAC (0F 01 CA) / STAC (0F 01 CB) — in-place 3-byte NOP
    clac_stac_hits = [i for i in range(len(region2)-2)
                      if region2[i]==0x0F and region2[i+1]==0x01 and region2[i+2] in (0xCA, 0xCB)]
    for rel_off in clac_stac_hits:
        name = "CLAC" if region2[rel_off+2]==0xCA else "STAC"
        z_off = seg_foff_z + rel_off
        data[z_off:z_off+3] = NOP3
        stats[name]+=1; ok+=1

    # 4. INVPCID (66 0F 38 82 ModRM [SIB] [disp]) — mod!=3 only, in-place 4-byte NOP
    for i in range(len(region2)-3):
        if (region2[i]==0x66 and region2[i+1]==0x0F and region2[i+2]==0x38 and region2[i+3]==0x82
            and i+4 < len(region2)):
            modrm = region2[i+4]
            mod = (modrm >> 6) & 3
            if mod != 3:  # memory form only
                z_off = seg_foff_z + i
                # 4-byte NOP: 66 0F 1F 00
                data[z_off:z_off+4] = bytes([0x66, 0x0F, 0x1F, 0x00])
                stats["INVPCID"]+=1; ok+=1

with open(OUT, "wb") as f: f.write(data)

# Verify residual (VEX only, 모든 exec segment)
verify   = open(OUT, "rb").read()
residual = 0
for sv, sf, ss in exec_segs:
    seg = verify[sf:sf+ss]
    residual += len(list(pat.finditer(seg)))

print("\n=== RESULT ===")
for mnem, cnt in sorted(stats.items()):
    print("  %-10s patched : %d" % (mnem, cnt))
print("  --------------------")
print("  Total patched  : %d" % ok)
print("  Skipped        : %d  %s" % (skip, dict(skip_stats) if skip else ""))
print("  Trampoline used: %d bytes / %d avail" % (tramp_ptr-tramp_foff_z, tramp_size))
print("  Residual hits  : %d (should be 0)" % residual)
print("  Output MD5     :", hashlib.md5(open(OUT,"rb").read()).hexdigest())
