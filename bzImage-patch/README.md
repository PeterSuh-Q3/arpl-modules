# BMI2 Ivy Bridge 패치 프로젝트 (실패 기록)

## 프로젝트 개요
- **목표**: Synology DSM 7.3 kernel을 Ivy Bridge(3세대 Intel CPU)에서 실행
- **문제**: Haswell 이상 CPU 기능(BMI1/BMI2/CLAC/STAC/INVPCID) 미지원
- **시도**: zImage 바이너리 패칭을 통한 비호환 명령어 트램폴린 교체

## 최종 결론: 실패

### 실패 원인

#### 1. **논리적 문제 vs 명령어 문제 - 근본적 오류 진단**
- **문제**: KX1234kekeke 패턴 반복 (부팅 완전 정지)
- **분석**: __early_make_pgtable() 함수의 **무한 재귀 루프**
- **원인**: 명령어 호환성 부족이 아닌 **kernel 초기화 로직 불일치**
  - Ivy Bridge: 특정 paging 초기화 순서/조건 미지원
  - 패치로 해결 불가능 (코드 로직 문제, 명령어가 아님)

#### 2. **v1-v5 패치 시도의 한계**

| 버전 | 시도 | 결과 | 원인 |
|------|------|------|------|
| v1-v4 | VEX BMI, MOVBE, XSAVES 패치 | KX1234kekeke (진전 무) | seg[3] 누락, MOVBE load 누락 등 |
| v5 | CLAC/STAC/INVPCID 추가 (115+50+2 패치) | 여전히 KX1234kekeke | 명령어가 아닌 로직 문제 |
| v5-norecurse | __early_make_pgtable 재귀 CALL → NOP | **KX1234** (진전!) | paging 초기화 불완전 → 부팅 중단 |
| v5-EMPT-RET | EMPT 함수 RET bypass (B8→C3) | **ke 무한반복 악화** | stack frame 설정 오류 |

#### 3. **bmi2_emul.ko의 한계**
- register_die_notifier()로 #UD 트랩 처리
- **문제**: Early boot stage에서 kernel 초기화 전
  - 모듈 로드 불가능
  - trap handler 등록 불가능
  - Runtime emulation 불가능

### 실제 동작하는 해결책

**해결**: Custom-kernel 사용 (문제 근원 차단)
```
Synology dsm_linux (5.10.55) 소스 
  ↓
-march=ivybridge로 재컴파일
  ↓
BMI1/BMI2/CLAC/STAC/INVPCID 없는 명령어만 생성
  ↓
Ivy Bridge에서 정상 부팅 ✓
```

**검증**: VM에서 custom-kernel + custom-modules 조합으로 정상 부팅 확인

## 디렉토리 구조

```
bzImage-patch/
├── 📋 README.md (본 파일)
├── 📋 FAILURE_ANALYSIS.md (상세 기술 분석)
├── 📋 bmi2_ivybridge_approach.md (진행 로그)
│
├── 🔧 patch_ivybridge.py (zImage 패치 스크립트)
├── 🔧 add_serial_probe.py (Boot trace 도구)
│
├── 📦 ext/ (Kernel binaries)
│   ├── extract_vmlinux.sh (커널 추출 유틸)
│   └── official-zImage/ (패치된 커널 바이너리)
│       ├── bzImage-epyc7002-7.3-5.10.55.gz (4.8M)
│       └── zImage-unified-*.gz (7.2M x 3)
│
└── 🛠️ vmlinux-build/ (Kernel build tools)
    ├── build-bmi2.sh (bmi2_emul 빌드)
    ├── build4.sh (kernel 빌드 스크립트)
    ├── hydrogen-stub.h (stub 헤더)
    └── bmi2_emul/ (BMI2 emulation module)
        ├── Makefile
        ├── bmi2_emul.c (소스, 14KB)
        └── bmi2_emul.ko (컴파일 바이너리, 313KB)
```

## 프로젝트 산출물

### 코드
- `patch_ivybridge.py` (v5): 8159개 명령어 패치 (무효)
- `add_serial_probe.py`: 상세 boot trace 수집 (진단 도구)
- `vmlinux-build/bmi2_emul.c`: BMI2 instruction emulation 모듈 (실패)
- `vmlinux-build/build4.sh`: Kernel 빌드 스크립트

### 학습 내용
- zImage 바이너리 패칭의 한계
- Early boot kernel 로직의 CPU 아키텍처 의존성
- Kernel symbol 호환성 vs 로직 호환성의 차이

### 실패 요인 분석
1. **기술적**: Early boot 코드는 binary patching 불가능한 영역
2. **아키텍처적**: Ivy Bridge는 paging 초기화 로직이 근본적으로 다름
3. **접근 방식**: zImage binary patch → kernel source rebuild로 전환

## 결과물 보존 이유
- 비슷한 문제 직면 시 참고 자료
- Binary patching의 한계 사례 기록
- Ivy Bridge 호환성 문제의 근본 원인 분석

---
**최종 결론**: Ivy Bridge DSM 7.3 운영 → custom-kernel (자체 컴파일) 필수
