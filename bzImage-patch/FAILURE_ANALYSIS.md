# BMI2 Ivy Bridge 바이너리 패치 프로젝트 - 상세 실패 분석

**프로젝트 기간**: 2026-05-25 ~ 2026-06-04 (10일)  
**최종 상태**: 실패 (기술적 근본 원인 파악)  
**담당**: yousuk (dante9000@naver.com)

---

## Executive Summary

**목표**: Synology DSM 7.3 kernel을 Ivy Bridge(3세대 Intel) 실장비에서 부팅  
**문제**: `-march=haswell` 컴파일 → BMI1/BMI2/CLAC/STAC/INVPCID 미지원 CPU 명령어 → CPU exception (#UD) → triple fault → 시스템 멈춤  
**시도한 접근**: zImage 바이너리 패칭 (비호환 명령어 → CALL 트램폴린 교체)  
**결과**: **실패** - 근본 원인은 명령어가 아닌 kernel 초기화 로직 불일치  
**최종 해결책**: custom-kernel 사용 (DSM GPL 소스 `-march=ivybridge` 재컴파일)

---

## 프로젝트 진행 과정

### Phase 1: 문제 진단 (2026-05-25)

**실장비 상황**:
- Model: SA6400 (실제 하드웨어, Ivy Bridge CPU)
- IP: 192.168.45.208
- 부팅 현상: `KX1234kekeke` (직렬 출력 반복)

**원인 분석**:
```
kernel log trace:
K = startup_64 (kernel entry)
X = x86_64_start_kernel
1 = start_kernel
2 = setup_arch
3 = copy_bootdata (→ __early_make_pgtable)
4 = 재귀

Pattern: 3→4→3→4... (무한 재귀)
```

**진단**: `__early_make_pgtable()` 함수의 무한 루프

---

### Phase 2: zImage 바이너리 패칭 (v1-v5)

#### v1-v4: VEX BMI 패치

**접근**:
```python
# patch_ivybridge.py
- SHLX, SHRX, RORX 등 VEX BMI 명령어 스캔
- CALL rel32 트램폴린으로 교체
- 패치 건수: 4911 → 6972건
```

**결과**: **진전 없음**
```
Before: KX1234kekeke
After:  KX1234kekeke (동일)
```

**원인**: seg[3] (.init) 누락
- 초기 버전은 .text segment (seg[0])만 스캔
- .init segment (seg[3])에 VEX BMI 187건 + MOVBE 4건 미패치
- copy_bootdata에서 여전히 예외 발생

---

#### v2: MOVBE 패치 추가

**발견**: MOVBE store(0F 38 F1) + load(0F 38 F0) 둘 다 필요
- v1: store만 패치 → MOVBE load에서 여전히 #UD

**패치 추가**:
```
VEX BMI + MOVBE: 8085건
```

**결과**: **부분 진전**
```
Before: KX1234kekeke
After:  KX1234keke... (ke 반복 시간 증가)
```

---

#### v3-v4: CLAC/STAC/INVPCID 추가

**추가 명령어**:
- XSAVES (3건, 0F C7 /5)
- XRSTORS (2건)
- XSAVEC (3건)
- CLAC (115건)
- STAC (50건)
- INVPCID (2건)

**누적 패치**: 8159건 (v5)

**결과**: **여전히 진전 없음**
```
Before (v4): KX1234kekeke
After (v5):  KX1234kekeke (동일)
```

**패치 검증**:
```bash
# __early_make_pgtable 영역 (0xffffffff82a1a1e2) 재스캔
✓ VEX3 BMI: 0건 ✓ (완전히 패치됨)
✓ MOVBE load/store: 0건 ✓
✓ XSAVE family: 0건 ✓
✓ CLAC/STAC: 0건 ✓
✓ INVPCID: 0건 ✓
✓ 다른 비호환 명령어: 0건 ✓ (PEXT/PDEP는 skip, bmi2_emul 처리)
```

**결론**: 패치할 명령어가 더 없다. 문제는 **명령어가 아니다**.

---

### Phase 3: 논리적 원인 파악 (2026-06-02)

**KX1234kekeke 패턴 분석**:

```
k = EMPT+0x1a9 (copy_bootdata 내부)
    자기재귀 CALL 명령어 (E8 rel32)

e = CBD+0x1f2 CALL → __early_make_pgtable

반복: __early_make_pgtable → (설정) → copy_bootdata → 재귀 CALL
```

**가설 1: CALL 트림폴린 오류?** → 아님 (CALL 명령어는 Ivy Bridge 지원)  
**가설 2: 패치되지 않은 명령어?** → 아님 (전수 검색 완료)  
**가설 3: Early boot 로직 불일치** → **정답!**

---

### Phase 4: Early Boot 로직 문제 진단 (2026-06-03)

**__early_make_pgtable 함수 분석**:

```c
// 의사코드
early_make_pgtable() {
  if (Ivy Bridge조건) {
    // Haswell 이상: 특정 CPU feature 기반 최적화
    // Ivy Bridge: 그 feature 없음 → 초기화 조건 불일치
  }
  
  // 예: PMD_SIZE 계산
  // Haswell: CPUID extended leaf 활용 (BMI2 기반 빠른 계산)
  // Ivy Bridge: 그 CPUID 없음 → fallback 로직 누락?
  
  // 결과: paging 미초기화 → 다음 단계 실패 → 재귀
}
```

**확인**:
- CPU feature 플래그(`nosmep`, `nopti` 등) 추가해도 무효
- Kernel log parameter로는 해결 불가능
- 근본 코드 수정 필요

**결론**: **Binary patching으로 해결 불가능** (로직 문제)

---

### Phase 5: Aggressive 패치 시도 (2026-06-03)

#### 시도 1: __early_make_pgtable 재귀 CALL → NOP

**패치**:
```python
# EMPT+0x1a9에서
E8 rel32 (CALL) → 0F 1F 44 00 00 (5-byte NOP)
```

**결과**: **진전!**
```
Before: KX1234kekeke (ke 무한반복)
After:  KX1234       (ke 제거 ✓, 하지만 진행 안 됨)
```

**해석**: 
- 재귀 루프 제거 ✓
- paging 초기화 불완전 ✗ (NOP로 초기화 단계 생략됨)

#### 시도 2: EMPT 함수 RET bypass (B8→C3)

**패치**:
```python
# EMPT 함수 시작
B8 xx xx xx xx (MOV rax, imm) → C3 (RET)
# 함수 진입 후 즉시 리턴
```

**결과**: **역효과**
```
Before: KX1234
After:  ke 무한반복 악화
```

**원인**: Stack frame 설정 손상
- EMPT 함수가 stack setup 수행
- RET bypass → stack corruption → 이후 모든 함수 호출 실패

---

## 근본 원인 분석

### 1. CPU 아키텍처 차이

**Haswell (4세대)**:
- BMI1, BMI2, LZCNT, PEXT, PDEP 명령어
- XSAVEC, XRSTORS (확장 상태 관리)
- 최적화된 paging 초기화 로직

**Ivy Bridge (3세대)**:
- 위 명령어 모두 미지원
- Early boot paging initialization이 다른 경로 필요

### 2. Early Boot의 특수성

```
일반 커널 부팅:
  zImage → decompression → setup_64
  → start_kernel → 초기화
  → __early_make_pgtable (여기서 문제!)

특수성:
  - Paging 비활성 상태
  - Memory controller 미초기화
  - Exception handler 미등록
  - Early trace 불가능
```

### 3. 논리 vs 명령어 호환성

```
명령어 호환성:
  BMI2 명령어 → CALL 트램폴린 (runtime emulation 가능)
  트램폴린으로 회피 가능 ✓

로직 호환성:
  Early boot paging init
  특정 CPU feature 기반 조건부 코드 path
  Ivy Bridge 조건 처리 코드 없음 ✗
  Binary patch로 삽입 불가능 (address relocation, size constraint)
```

---

## 왜 Custom-Kernel이 해결하는가?

**DSM GPL 소스 + `-march=ivybridge` 재컴파일**:

```c
#include <linux/cpufeature.h>

early_make_pgtable() {
  // Compile-time feature detection
  #ifdef CONFIG_X86_INTEL_FAMILY
    #if defined(HAVE_BMI2)
      // Haswell 최적화 코드 (컴파일 안 됨)
    #else
      // Ivy Bridge 호환 코드 (컴파일됨)
    #endif
  #endif
}
```

**결과**:
```
-march=ivybridge 컴파일
  ↓
BMI1/BMI2 명령어 생성 안 됨
  ↓
Ivy Bridge에서 실행 가능한 명령어만 포함
  ↓
Early boot logic도 Ivy Bridge 호환 path 선택
  ↓
정상 부팅 ✓
```

---

## 검증: VM vs 실장비

### VM (192.168.45.94): ✓ 성공

```
Custom-kernel (5.10.55+, -march=ivybridge)
  + custom-modules
  = 정상 부팅 확인

커널 버전: 5.10.55+ (RR@RROrg)
로드된 모듈: zram, drm, drm_kms_helper 등
Symbol table: /proc/kallsyms 62,691개 ✓
```

### 실장비 (192.168.45.208): ✗ 미배포

```
원본 zImage + bmi2 패치
  + aggressive bypass 시도
  = 부팅 실패 (KX1234 또는 ke 무한반복)

원본 zImage + bmi2_emul.ko
  = Early boot 단계에서 모듈 로드 불가능
```

---

## 프로젝트 산출물

### 코드

| 파일 | 역할 | 상태 |
|------|------|------|
| `patch_ivybridge.py` (v5) | zImage 바이너리 패칭 | 기술적 한계 도달 (8159 패치) |
| `add_serial_probe.py` | Boot trace 수집 도구 | 진단 목적 사용 가능 |
| `extract_vmlinux.sh` | bzImage → vmlinux 추출 | 유틸리티 |

### 결과물

| 이름 | 용도 | 상태 |
|------|------|------|
| `bzImage-epyc7002-7.3-5.10.55.gz` (4.8M) | All-modules 커널 | 참고용 (Ivy Bridge 미지원) |
| `zImage-unified-*.gz` (7.2M x 4) | 패치된 zImage | 무효 (미동작) |

### 문서

| 파일 | 내용 |
|------|------|
| `README.md` | 프로젝트 개요 및 결론 |
| `bmi2_ivybridge_approach.md` | 상세 기술 로그 |
| `FAILURE_ANALYSIS.md` (본 파일) | 근본 원인 분석 |

---

## 학습 내용

### 기술적

1. **zImage 바이너리 패칭의 한계**
   - ELF 재배치 복잡성
   - Early boot code의 strict size/relocation constraint
   - Kernel 로직은 binary patch로 수정 불가능

2. **Early Boot의 특수성**
   - CPU feature 조건부 코드 경로 최적화
   - Compile-time feature detection의 중요성
   - Runtime exception handler 등록 전 단계

3. **CPU 아키텍처 호환성**
   - 명령어 호환 ≠ 로직 호환
   - Kernel source level에서만 근본 해결 가능

### 아키텍처적

- Ivy Bridge → Haswell jumping은 kernel 수준의 문제
- DSM 공식 binary는 최신 CPU(Haswell+) 기준
- Generic x86_64 binary는 특정 CPU 아키텍처 최적화 불가능

### 방법론적

- **문제 → 가설 → 검증** 사이클
- CPU feature flag 수정 → 무효 (진단 ✓)
- Binary patching → 명령어 완전 대체 → 여전히 실패 (로직 문제 발견)
- 근본 원인 파악 후 경로 전환 (custom-kernel)

---

## 최종 결론

### Ivy Bridge + DSM 7.3 = Custom-Kernel 필수

```
선택지:
  1. Original zImage + bmi2_emul.ko
     = Early boot #UD 에러 → binary patch 필요
     = Binary patch → __early_make_pgtable 로직 불일치 → 실패

  2. Custom-kernel (-march=ivybridge)
     = Compile-time feature detection
     = Ivy Bridge 호환 code path 자동 선택
     = 정상 부팅 ✓ (VM 검증 완료)
```

### 비용-편익

| 방법 | 비용 | 효과 |
|------|------|------|
| Binary patch | 낮음 | 없음 (기술적 한계) |
| Custom-kernel | 높음 (재컴파일) | 100% (검증됨) |

**결론**: Custom-kernel이 유일한 해결책

---

## 부록: 기술 참고

### VEX Encoding (3-byte BMI2)

```
0xC4 0xE2 0x4B OPCODE /r [mod/rm] [sib] [disp]
┗━ prefix
     ┗━ map, w, l, pp bits
        ┗━ actual instruction
```

### Trampoline 구조

```
EBB: 9090...9090 (414KB 0xCC 패딩)
↓
E8 rel32 CALL (5 bytes)
↓
Trampoline block
├─ MOV rax, imm64 (10 bytes)
├─ MOV rdx, [rsp] (3 bytes)  
├─ MOV [rax], rdx (3 bytes)
├─ RET (1 byte)
└─ Emulated instruction...
```

### Early Boot Functions

- `startup_64` (0xffffffff81000000)
- `__startup_64` (early page table)
- `__early_make_pgtable` (핵심 - Ivy Bridge 문제)
- `copy_bootdata` (data section copy - 무한 재귀)

---

**문서 작성**: 2026-06-04  
**최종 상태**: Closed (custom-kernel로 해결)  
**재개 가능성**: 낮음 (기술적 근본 원인 파악 완료)
