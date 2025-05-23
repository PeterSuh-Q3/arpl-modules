#!/usr/bin/env bash

set -e

TOOLKIT_VER="7.1"
PLATFORM="bromolow"
KVER="3.10.108"
#if [ -f ../../arpl/PLATFORMS ]; then
#  cp ../../arpl/PLATFORMS PLATFORMS
#else
#  curl -sLO "https://github.com/fbelavenuto/arpl/raw/main/PLATFORMS"
#fi

echo -e "Compiling modules..."
  DIR="${KVER:0:1}.x"
  [ ! -d "${PWD}/${DIR}" ] && continue
  mkdir -p "/tmp/${PLATFORM}-${KVER}"
  #docker run --rm -t -v "${PWD}/${1}/${DIR}":/input -v "${PWD}/../${PLATFORM}-${KVER}":/output \
  #  fbelavenuto/syno-toolkit:${PLATFORM}-${TOOLKIT_VER} compile-module
  runparam=$(echo "-u `id -u` --rm -t -v "${PWD}/${DIR}":/input -v "/tmp/${PLATFORM}-${KVER}":/output \
    fbelavenuto/syno-compiler:${TOOLKIT_VER} compile-module ${PLATFORM}")
  echo $runparam  
  docker run -u `id -u` --rm -t -v "${PWD}/${DIR}":/input -v "/tmp/${PLATFORM}-${KVER}":/output \
    fbelavenuto/syno-compiler:${TOOLKIT_VER} compile-module ${PLATFORM}
  for M in `ls /tmp/${PLATFORM}-${KVER}`; do
      if [ "${PLATFORM}" = "epyc7002" ]; then
        PLATFORM_DIR="${PLATFORM}-${TOOLKIT_VER}-${KVER}"
      else
        PLATFORM_DIR="${PLATFORM}-${KVER}"
      fi  
    [ -f ~/src/pats/modules/${PLATFORM}/$M ] && \
      # original      
      cp ~/src/pats/modules/${PLATFORM}/$M "${PWD}/../${PLATFORM_DIR}" || \
      # compiled
      cp /tmp/${PLATFORM}-${KVER}/$M "${PWD}/../${PLATFORM_DIR}"
      # remove i915 related modules
      [[ -f ${PWD}/../${PLATFORM}-${KVER}/cfbfillrect.ko ]] && rm ${PWD}/../${PLATFORM_DIR}/cfbfillrect.ko 
      [[ -f ${PWD}/../${PLATFORM}-${KVER}/cfbimgblt.ko ]] && rm ${PWD}/../${PLATFORM_DIR}/cfbimgblt.ko 
      [[ -f ${PWD}/../${PLATFORM}-${KVER}/cfbcopyarea.ko ]] && rm ${PWD}/../${PLATFORM_DIR}/cfbcopyarea.ko 
      [[ -f ${PWD}/../${PLATFORM}-${KVER}/video.ko ]] && rm ${PWD}/../${PLATFORM_DIR}/video.ko 
      [[ -f ${PWD}/../${PLATFORM}-${KVER}/backlight.ko ]] && rm ${PWD}/../${PLATFORM_DIR}/backlight.ko 
      [[ -f ${PWD}/../${PLATFORM}-${KVER}/button.ko ]] && rm ${PWD}/../${PLATFORM_DIR}/button.ko 
      [[ -f ${PWD}/../${PLATFORM}-${KVER}/drm_kms_helper.ko ]] && rm ${PWD}/../${PLATFORM_DIR}/drm_kms_helper.ko 
      [[ -f ${PWD}/../${PLATFORM}-${KVER}/drm.ko ]] && rm ${PWD}/../${PLATFORM_DIR}/drm.ko 
      [[ -f ${PWD}/../${PLATFORM}-${KVER}/fb.ko ]] && rm ${PWD}/../${PLATFORM_DIR}/fb.ko 
      [[ -f ${PWD}/../${PLATFORM}-${KVER}/fbdev.ko ]] && rm ${PWD}/../${PLATFORM_DIR}/fbdev.ko 
      [[ -f ${PWD}/../${PLATFORM}-${KVER}/i2c-algo-bit.ko ]] && rm ${PWD}/../${PLATFORM_DIR}/i2c-algo-bit.ko
  done
  rm -rf /tmp/${PLATFORM}-${KVER}
