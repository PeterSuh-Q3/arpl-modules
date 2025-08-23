# Dockerfile.synology-build
FROM ubuntu:22.04

# 필수 도구 설치
RUN apt-get update && apt-get install -y \
    git python3 build-essential sudo \
    && rm -rf /var/lib/apt/lists/*

# pkgscripts-ng 클론 및 DSM 브랜치 체크아웃
WORKDIR /opt
RUN git clone https://github.com/SynologyOpenSource/pkgscripts-ng.git
WORKDIR /opt/pkgscripts-ng
RUN git checkout DSM7.2

# Synology 툴체인 다운로드·압축 해제
RUN sudo ./EnvDeploy -q -v 7.2 -p v1000nk

# 빌드 환경 디렉토리 위치 환경 변수 설정
ENV BUILD_ENV=/opt/build_env/ds.v1000nk-7.2
