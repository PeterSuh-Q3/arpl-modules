# BMI2 Ivy Bridge 대응 접근법 및 분석

## 배경
- Synology DSM 7.3 epyc7002/geminilakenk 커널은 `-march=haswell`로 컴파일
- Ivy Bridge(3세대) CPU는 BMI1/BMI2/MOVBE/XSAVES 미지원 → `#UD` → Triple Fault → 시리얼 완전 침묵
- SA6400 실 vmlinux 분석: 비압축 ELF 33MB, EXEC segment 17MB

## GPL 커널 접근 (폐기)
- GPL 소스 + `-march=ivybridge` 빌드 → Synology 바이너리 모듈 구조체 불일치 → probe crash
- 결론: Synology kpatched 바이너리 커널 유지 필수

## 채택 접근: 이중 전략
1. **bmi2_emul.ko**: `register_die_notifier()` → `#UD` 트랩 → 런타임 에뮬레이션 (후기 로드)
2. **zImage 바이너리 패치**: startup_64~earlycon 이전 실행 명령어를 CALL 트램폴린으로 교체

## 통합 패치 스크립트 (현재 버전)
- **파일**: `redpill-load/src/patch_ivybridge.py`
- **입력**: 원본 zImage (미패치), **출력**: 완전 패치 zImage
- **사용법**: `python3 patch_ivybridge.py <input> <output>`
- **최신 커밋**: `d639d90` (redpill-load/master)

### 패치 결과 (원본 기준, 2026-06-03)
| 명령어 | 건수 | 분류 |
|---|---|---|
| SHLX | 2444 | BMI2 VEX |
| SHRX | 1589 | BMI2 VEX |
| RORX | 2015 | BMI2 VEX (pp=3 버그 수정 포함) |
| SARX | 285 | BMI2 VEX |
| MULX | 46 | BMI2 VEX (red-zone 트램폴린) |
| BZHI | 74 | BMI2 VEX |
| ANDN | 529 | BMI1 VEX |
| BLSR | 57 | BMI1 VEX |
| BLSI | 4 | BMI1 VEX |
| BLSMSK | 3 | BMI1 VEX |
| MOVBE_st | 1105 | 비-VEX (0F 38 F1) |
| XSAVES | 3 | 비-VEX (0F C7 /5) → NOP |
| XRSTORS | 2 | 비-VEX (0F C7 /3) → NOP |
| XSAVEC | 3 | 비-VEX (0F C7 /4) → NOP |
| **총계** | **8159** | |

- 잔여 skip: PEXT/PDEP/BEXTR 120건 (256KB+, bmi2_emul.ko 처리)
- 최신 패치본 MD5: `5f8a09a4c8bc9564b30fcfec8617646c`
- gz 백업: `ext/official-zImage/zImage-unified-epyc7002-7.3-5.10.55.gz`

### 트램폴린 구조
- 0xCC 패딩 블록(426KB) 자동 탐지 → CALL rel32 스텁 삽입
- vaddr: 0xffffffff817956b5, 사용: ~82KB / 426KB 가용

### 주요 버그 발견 이력
1. **RORX pp 오타**: `(3,2,0xF0)` → 올바른 `(3,3,0xF0)` (F2=pp=3). 2015건 누락됐었음
2. **비-VEX 명령어 누락**: 초기 스캔이 0xC4(VEX)만 탐색 → MOVBE/XSAVES/XRSTORS 누락
3. **VEX b1 스캔 범위**: 이전 `[\xe2\xe3]`만 검사 → r8~r15 사용 명령어 1285건 누락

### 0~256KB 검증 결과 (최신 패치 후)
- VEX BMI1/BMI2: 0건 ✅
- MOVBE: 0건 ✅ (2건 패치됨: 192KB, 196KB)
- XSAVES/XRSTORS: 0건 ✅ (4건 패치됨: 171~173KB)
- BZHI: 0건 ✅ (0~256KB에는 원래 없었음)
- EVEX(0x62): 132 hits → **전부 false positive** (immediate/displacement 바이트)
- FMA3: 0건 ✅, AVX2: 0건 ✅, ADCX/ADOX: 0건 ✅

## 패치 스크립트 버그 이력

### v1 → v2: seg[3] 누락
- patch_ivybridge.py가 seg[0](.text 17MB)만 스캔, seg[3](.init 1208KB) 미패치
- seg[3]에 VEX BMI 187건 + MOVBE 4건 → copy_bootdata에서 크래시 (KX1234 멈춤)
- 수정: `find_all_exec_segments()` 추가, 모든 exec segment 스캔

### v2 → v3: MOVBE_ld 누락
- patch_ivybridge.py가 MOVBE store(0F 38 F1)만 패치, MOVBE load(0F 38 F0) 미패치
- seg[0]: 782건, seg[3]: 46건 → __early_make_pgtable 실행 중 크래시 (KX1234keke)
- 수정: decode_movbe(opcode), make_movbe_load_trampoline() 추가

### v3 → v4: SIB 인코딩 오류 + 4바이트 MOVBE 미처리
- decode_movbe가 SIB 바이트를 읽지만 저장 안 함 → trampoline에서 0x24(base only)로 잘못 인코딩 (225건 영향)
- insn_len<5(4바이트 MOVBE) 123건 skip → 그냥 누락
- 수정: `encode_mem_ref()` SIB 완전 지원, 4바이트 MOVBE는 다음 1바이트와 함께 패치

## 현재 상태 (2026-06-03 v5 진행 중)
- **v5 패치본 배포 (2026-06-03 최신)**
  - VEX BMI seg[0]+seg[3]: 8368건
  - MOVBE_ld: 826건, MOVBE_st: 21건
  - **CLAC: 115건, STAC: 50건, INVPCID: 2건 추가**
  - **총 1216건 추가**, 전체 ~9424건 패치
  - Trampoline 사용: 7944/354801 bytes
  - probe 포함 배포본: MD5 `3cfa46335d86d85ba6feea849bdeef99`
  - 실장비 재부팅 결과: **KX1234kekeke** (여전히 같은 패턴)

- **진단 결과**:
  - v4/v5에서 __early_make_pgtable (0xffffffff82a1a1e2) 영역 상세 스캔
  - VEX3 BMI: 0건 ✓
  - MOVBE load/store: 0건 ✓
  - XSAVE family: 0건 ✓
  - LZCNT (F3 0F BD): 0건
  - RDRAND/RDSEED: 0건
  - **결론**: 패치 불가능한 명령어 미검출 → 다른 원인 추정

- **"kekeke" 반복 패턴 분석**:
  - k = EMPT+0x1a9 자기재귀 CALL → __early_make_pgtable 재진입
  - e = CBD+0x1f2 CALL → __early_make_pgtable (0xffffffff82a1a1e2)
  - 반복: __early_make_pgtable → copy_bootdata → __early_make_pgtable → ...
  - 가능성 1: 무한 재귀 루프 (명령어 아님, 로직)
  - 가능성 2: 예상 밖 CALL 타겟 0xffffffff6ba1a3af 등 실패
  - 가능성 3: 패치되지 않은 특수 명령어 (LOCK + 특정 opcode, ADC, SBB 등)

## 진행 상황 (2026-06-04, aggressive 패치 시도)

### 시도 1: kernel command line parameter (실패)
- `nosmep nopti nospec_store_bypass_disable` 추가
- 결과: 여전히 KX1234kekeke (CPU feature와 무관)

### 시도 2: EMPT+0x1a9 재귀 CALL → NOP (부분 성공)
- 패치: E8 rel32 → 0F 1F 44 00 00 (5-byte NOP)
- 결과: **KX1234** (ke 반복 제거 ✓)
- 문제: 여전히 그 이후 진행 안 됨 → paging 초기화 불완전 추정

### 시도 3: EMPT 함수 전체 bypass (역효과)
- 패치: 첫 바이트 B8 → C3 (RET)
- 결과: **ke 무한반복 악화** (stack frame 설정 안 됨)
- 상태: 실장비 crash/hang, 롤백 중

### bmi2_emul.ko 검증 (2026-06-04)
- 위치: initrd-dsm의 `usr/lib/modules/bmi2_emul.ko`
- MD5: `f5886361e5a7a29cdc5061202a6a57e8` ✓ (최신 버전)
- 상태: initrd-dsm에 이미 포함, 추가 수정 불필요
- **문제**: early boot 단계에서는 bmi2_emul.ko 로드 불가능

### 현재 결론
- zImage aggressive 패치 시도 → 위험성 ↑, 효과 미약
- early boot paging 초기화 문제는 **커널 코드 수정 필요** (불가능)
- 현실적 해결: 조건부 기능성 또는 다른 방식 커널 사용 필요

## 실장비 환경
- IP: `192.168.45.208`, 계정: `tc/P@ssw0rd`, sudo NOPASSWD
- TinyCore Linux, /mnt/sdb1~3 마운트됨
- 원본 zImage 백업: `/mnt/sdb3/zImage-dsm_` (MD5: `529077c7...`)
- VM(분석용): `192.168.45.94` = VMware Fusion (i7-10700, NOT 베어메탈)

## bmi2_emul.ko
- 위치: `redpill-load/src/bmi2_emul/`
- vermagic: `5.10.55+ SMP mod_unload` (epyc7002/geminilakenk 공용)
- emul_count module_param 포함 (`cat /sys/module/bmi2_emul/parameters/emul_count`)
- tinycore-redpill `functions_t.sh` buildloader()에 복사 로직 포함 (커밋 `6877a750`)

## 관련 커밋 (redpill-load/master)
- `494ecf9`: bmi2_emul emul_count module_param
- `a0abeaf`: patch_ivybridge.py 통합 (4911건, 초기)
- `6ff277e`: RORX pp 버그 수정 + MULX 트램폴린 (6972건)
- `052becf`: MOVBE/XSAVES/XRSTORS 비-VEX 패치 추가 (8085건)
- `d639d90`: BZHI 트램폴린 추가 (8159건, 현재 최신)
- `3e73021`: 중간 스크립트 정리 (patch_bmi2*.py 등 삭제)
