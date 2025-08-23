FROM ubuntu:22.04

# APT 후처리 및 docker-clean 비활성화
RUN printf 'APT::Update::Post-Invoke ""; APT::Update::Post-Invoke-Success "";' \
      > /etc/apt/apt.conf.d/99disable-postinvoke && \
    rm -f /etc/apt/apt.conf.d/docker-clean

# IPv4 강제 및 필수 패키지 설치
RUN printf 'Acquire::ForceIPv4 "true";' > /etc/apt/apt.conf.d/99force-ipv4 && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
      git \
      ca-certificates \
      python3 \
      build-essential \
      sudo && \
    rm -rf /var/lib/apt/lists/*

# pkgscripts-ng 소스 복사 및 DSM 브랜치 체크아웃
WORKDIR /opt
COPY pkgscripts-ng /opt/pkgscripts-ng
WORKDIR /opt/pkgscripts-ng
RUN git checkout DSM7.2

ARG DSM_VERSION=7.2
ENV DSM_VERSION=${DSM_VERSION}

# 각 플랫폼별 툴체인 다운로드·압축 해제 (하드코딩, sudo 제거)
RUN ./EnvDeploy -q -v ${DSM_VERSION} -p v1000nk && \
    rm -f /opt/toolkit_tarballs/*.txz && \
    ./EnvDeploy -q -v ${DSM_VERSION} -p r1000nk && \
    rm -f /opt/toolkit_tarballs/*.txz && \
    ./EnvDeploy -q -v ${DSM_VERSION} -p geminilakenk && \
    rm -f /opt/toolkit_tarballs/*.txz

# 빌드 환경 베이스 경로
ENV BUILD_ENV_BASE=/opt/build_env

