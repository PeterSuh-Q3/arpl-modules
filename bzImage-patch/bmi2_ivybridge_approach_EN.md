# BMI2 Ivy Bridge Approach and Analysis

## Background
- Synology DSM 7.3 epyc7002/geminilakenk kernel compiled with `-march=haswell`
- Ivy Bridge (3rd generation) CPU unsupported: BMI1/BMI2/MOVBE/XSAVES → `#UD` → Triple Fault → serial complete silence
- SA6400 real vmlinux analysis: uncompressed ELF 33MB, EXEC segment 17MB

## GPL Kernel Approach (Abandoned)
- GPL source + `-march=ivybridge` build → Synology binary module structure mismatch → probe crash
- Conclusion: Must maintain Synology kpatched binary kernel

## Adopted Approach: Dual Strategy
1. **bmi2_emul.ko**: `register_die_notifier()` → `#UD` trap → runtime emulation (late load)
2. **zImage binary patch**: Replace unsupported instructions (startup_64~earlycon) with CALL trampolines

## Unified Patch Script (Current Version)
- **File**: `redpill-load/src/patch_ivybridge.py`
- **Input**: Original zImage (unpatched), **Output**: Fully patched zImage
- **Usage**: `python3 patch_ivybridge.py <input> <output>`
- **Latest Commit**: `d639d90` (redpill-load/master)

### Patch Results (Baseline: 2026-06-03)
| Instruction | Count | Category |
|---|---|---|
| SHLX | 2444 | BMI2 VEX |
| SHRX | 1589 | BMI2 VEX |
| RORX | 2015 | BMI2 VEX (pp=3 bug fix included) |
| SARX | 285 | BMI2 VEX |
| MULX | 46 | BMI2 VEX (red-zone trampoline) |
| BZHI | 74 | BMI2 VEX |
| ANDN | 529 | BMI1 VEX |
| BLSR | 57 | BMI1 VEX |
| BLSI | 4 | BMI1 VEX |
| BLSMSK | 3 | BMI1 VEX |
| MOVBE_st | 1105 | non-VEX (0F 38 F1) |
| XSAVES | 3 | non-VEX (0F C7 /5) → NOP |
| XRSTORS | 2 | non-VEX (0F C7 /3) → NOP |
| XSAVEC | 3 | non-VEX (0F C7 /4) → NOP |
| **Total** | **8159** | |

- Remaining skip: PEXT/PDEP/BEXTR 120 instances (256KB+, handled by bmi2_emul.ko)
- Latest patch MD5: `5f8a09a4c8bc9564b30fcfec8617646c`
- gz backup: `ext/official-zImage/zImage-unified-epyc7002-7.3-5.10.55.gz`

### Trampoline Structure
- 0xCC padding block (426KB) auto-detection → CALL rel32 stub insertion
- vaddr: 0xffffffff817956b5, usage: ~82KB / 426KB available

### Major Bug Discovery History
1. **RORX pp typo**: `(3,2,0xF0)` → correct `(3,3,0xF0)` (F2=pp=3). 2015 instances missed
2. **non-VEX instruction missing**: Early scan only detected 0xC4 (VEX) → MOVBE/XSAVES/XRSTORS missed
3. **VEX b1 scan range**: Previous `[\xe2\xe3]` only checked → 1285 r8~r15 usage instructions missed

### 0~256KB Verification Results (After Latest Patch)
- VEX BMI1/BMI2: 0 instances ✅
- MOVBE: 0 instances ✅ (2 patched: 192KB, 196KB)
- XSAVES/XRSTORS: 0 instances ✅ (4 patched: 171~173KB)
- BZHI: 0 instances ✅ (originally absent in 0~256KB)
- EVEX (0x62): 132 hits → **all false positives** (immediate/displacement bytes)
- FMA3: 0 instances ✅, AVX2: 0 instances ✅, ADCX/ADOX: 0 instances ✅

## Patch Script Bug History

### v1 → v2: seg[3] Missing
- patch_ivybridge.py scanned only seg[0] (.text 17MB), missed seg[3] (.init 1208KB)
- seg[3] contains 187 VEX BMI + 4 MOVBE → crash in copy_bootdata (KX1234 halt)
- Fix: Added `find_all_exec_segments()`, scan all exec segments

### v2 → v3: MOVBE_ld Missing
- patch_ivybridge.py patched only MOVBE store (0F 38 F1), missed MOVBE load (0F 38 F0)
- seg[0]: 782 instances, seg[3]: 46 instances → crash during __early_make_pgtable (KX1234keke)
- Fix: Added decode_movbe(opcode), make_movbe_load_trampoline()

### v3 → v4: SIB Encoding Error + 4-byte MOVBE Unhandled
- decode_movbe read SIB byte but didn't store → trampoline incorrectly encoded as 0x24 (base only) (225 affected)
- insn_len<5 (4-byte MOVBE) 123 instances skipped → simply omitted
- Fix: Complete SIB support in `encode_mem_ref()`, patch 4-byte MOVBE with following 1 byte

## Current Status (2026-06-03 v5 In Progress)
- **v5 Patch Distribution (2026-06-03 Latest)**
  - VEX BMI seg[0]+seg[3]: 8368 instances
  - MOVBE_ld: 826 instances, MOVBE_st: 21 instances
  - **CLAC: 115, STAC: 50, INVPCID: 2 added**
  - **Total 1216 added**, overall ~9424 patches
  - Trampoline usage: 7944/354801 bytes
  - Distribution with probe: MD5 `3cfa46335d86d85ba6feea849bdeef99`
  - Real hardware reboot result: **KX1234kekeke** (same pattern still)

- **Diagnosis Results**:
  - Detailed scan of __early_make_pgtable (0xffffffff82a1a1e2) region in v4/v5
  - VEX3 BMI: 0 instances ✓
  - MOVBE load/store: 0 instances ✓
  - XSAVE family: 0 instances ✓
  - LZCNT (F3 0F BD): 0 instances
  - RDRAND/RDSEED: 0 instances
  - **Conclusion**: No unpatched instructions detected → different cause suspected

- **"kekeke" Repetition Pattern Analysis**:
  - k = EMPT+0x1a9 self-recursive CALL → __early_make_pgtable reentry
  - e = CBD+0x1f2 CALL → __early_make_pgtable (0xffffffff82a1a1e2)
  - Loop: __early_make_pgtable → copy_bootdata → __early_make_pgtable → ...
  - Possibility 1: Infinite recursion loop (logic, not instruction)
  - Possibility 2: Unexpected CALL target 0xffffffff6ba1a3af etc failure
  - Possibility 3: Unpatched special instruction (LOCK + specific opcode, ADC, SBB, etc)

## Progress (2026-06-04, Aggressive Patch Attempts)

### Attempt 1: Kernel Command Line Parameter (Failed)
- Added `nosmep nopti nospec_store_bypass_disable`
- Result: Still KX1234kekeke (CPU feature independent)

### Attempt 2: EMPT+0x1a9 Recursion CALL → NOP (Partial Success)
- Patch: E8 rel32 → 0F 1F 44 00 00 (5-byte NOP)
- Result: **KX1234** (ke repetition removed ✓)
- Problem: Still no further progress → incomplete paging initialization suspected

### Attempt 3: EMPT Function Complete Bypass (Counterproductive)
- Patch: First byte B8 → C3 (RET)
- Result: **ke infinite repeat worsened** (stack frame setup failed)
- Status: Real hardware crash/hang, rollback in progress

### bmi2_emul.ko Verification (2026-06-04)
- Location: `usr/lib/modules/bmi2_emul.ko` in initrd-dsm
- MD5: `f5886361e5a7a29cdc5061202a6a57e8` ✓ (latest version)
- Status: Already included in initrd-dsm, no further modification needed
- **Problem**: Cannot load bmi2_emul.ko at early boot stage

### Current Conclusion
- zImage aggressive patching attempt → increased risk, marginal effect
- Early boot paging initialization problem → **kernel code modification required** (impossible)
- Practical solution: Conditional functionality or alternative kernel approach needed

## Real Hardware Environment
- IP: `192.168.45.208`, account: `tc/P@ssw0rd`, sudo NOPASSWD
- TinyCore Linux, /mnt/sdb1~3 mounted
- Original zImage backup: `/mnt/sdb3/zImage-dsm_` (MD5: `529077c7...`)
- VM (analysis): `192.168.45.94` = VMware Fusion (i7-10700, NOT bare metal)

## bmi2_emul.ko
- Location: `redpill-load/src/bmi2_emul/`
- vermagic: `5.10.55+ SMP mod_unload` (shared by epyc7002/geminilakenk)
- Includes emul_count module_param (`cat /sys/module/bmi2_emul/parameters/emul_count`)
- Copy logic included in tinycore-redpill `functions_t.sh` buildloader() (commit `6877a750`)

## Related Commits (redpill-load/master)
- `494ecf9`: bmi2_emul emul_count module_param
- `a0abeaf`: patch_ivybridge.py integration (4911 instances, initial)
- `6ff277e`: RORX pp bug fix + MULX trampoline (6972 instances)
- `052becf`: MOVBE/XSAVES/XRSTORS non-VEX patch addition (8085 instances)
- `d639d90`: BZHI trampoline addition (8159 instances, current latest)
- `3e73021`: Intermediate script cleanup (deleted patch_bmi2*.py etc)
