# BMI2 Ivy Bridge Patch Project (Failure Record)

## Project Overview
- **Objective**: Run Synology DSM 7.3 kernel on Ivy Bridge (3rd generation Intel CPU) systems
- **Problem**: Haswell and later CPU features (BMI1/BMI2/CLAC/STAC/INVPCID) not supported
- **Attempt**: Binary zImage patching to replace incompatible instruction trampolines

## Final Conclusion: Failed

### Failure Reasons

#### 1. **Logic Problem vs Instruction Problem - Root Cause Diagnosis**
- **Problem**: KX1234kekeke pattern repetition (complete boot halt)
- **Analysis**: **Infinite recursion loop** in `__early_make_pgtable()` function
- **Root Cause**: Not instruction compatibility shortage, but **kernel initialization logic mismatch**
  - Ivy Bridge: Specific paging initialization sequence/condition not supported
  - Cannot be solved by patching (code logic issue, not instruction-level)

#### 2. **v1-v5 Patch Attempts and Their Limitations**

| Version | Attempt | Result | Reason |
|---------|---------|--------|--------|
| v1-v4 | VEX BMI, MOVBE, XSAVES patches | KX1234kekeke (no progress) | seg[3] missing, MOVBE load missing, etc |
| v5 | CLAC/STAC/INVPCID added (115+50+2 patches) | Still KX1234kekeke | Logic problem, not instruction issue |
| v5-norecurse | __early_make_pgtable recursion CALL → NOP | **KX1234** (progress!) | Paging initialization incomplete → boot halt |
| v5-EMPT-RET | EMPT function RET bypass (B8→C3) | **ke infinite repeat worsened** | Stack frame setup corruption |

#### 3. **bmi2_emul.ko Limitations**
- Trap handler for #UD exceptions via register_die_notifier()
- **Problem**: Before kernel initialization in early boot stage
  - Module loading impossible
  - Trap handler registration impossible
  - Runtime emulation impossible

### Actual Working Solution

**Solution**: Custom-kernel usage (eliminate root cause)
```
Synology dsm_linux (5.10.55) source
  ↓
Recompile with -march=ivybridge
  ↓
Generate only instructions without BMI1/BMI2/CLAC/STAC/INVPCID
  ↓
Normal boot on Ivy Bridge ✓
```

**Verification**: Confirmed normal boot on VM with custom-kernel + custom-modules combination

## Directory Structure

```
bzImage-patch/
├── 📋 README.md (Korean version)
├── 📋 README_EN.md (This file)
├── 📋 FAILURE_ANALYSIS.md (Korean, detailed technical analysis)
├── 📋 FAILURE_ANALYSIS_EN.md (English version)
├── 📋 bmi2_ivybridge_approach.md (Korean, progress log)
│
├── 🔧 patch_ivybridge.py (zImage patching script)
├── 🔧 add_serial_probe.py (Boot trace tool)
│
├── 📦 ext/ (Kernel binaries)
│   ├── extract_vmlinux.sh (Kernel extraction utility)
│   └── official-zImage/ (Patched kernel binaries)
│       ├── bzImage-epyc7002-7.3-5.10.55.gz (4.8M)
│       └── zImage-unified-*.gz (7.2M x 3)
│
└── 🛠️ vmlinux-build/ (Kernel build tools)
    ├── build-bmi2.sh (bmi2_emul build script)
    ├── build4.sh (Kernel build script)
    ├── hydrogen-stub.h (Stub header)
    └── bmi2_emul/ (BMI2 emulation module)
        ├── Makefile
        ├── bmi2_emul.c (Source code, 14KB)
        └── bmi2_emul.ko (Compiled binary, 313KB)
```

## Project Deliverables

### Code
- `patch_ivybridge.py` (v5): 8159 instruction patches (ineffective)
- `add_serial_probe.py`: Detailed boot trace collection (diagnostic tool)
- `vmlinux-build/bmi2_emul.c`: BMI2 instruction emulation module (failed approach)
- `vmlinux-build/build4.sh`: Kernel build script

### Learning Points
- Limitations of zImage binary patching
- CPU architecture dependency of early boot kernel logic
- Difference between kernel symbol compatibility vs logic compatibility

### Failure Factor Analysis
1. **Technical**: Early boot code is a region impossible to patch by binary modification
2. **Architectural**: Ivy Bridge has fundamentally different paging initialization logic
3. **Approach**: Transition from zImage binary patch → kernel source rebuild

## Reasons for Preserving Results
- Reference material for similar issues in the future
- Case study of binary patching limitations
- Root cause analysis of Ivy Bridge compatibility issues

---
**Final Conclusion**: Ivy Bridge DSM 7.3 operation → custom-kernel (self-compiled) essential
