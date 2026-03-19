# SPDX-License-Identifier: GPL-2.0-only


# Skip dma-buf when building external modules (DSM policy)
ifneq ($(KBUILD_EXTMOD),)

$(info Skipping dma-buf external module build on DSM)

else

obj-$(CONFIG_DMA_SHARED_BUFFER) := dma-buf.o dma-fence.o dma-fence-array.o dma-fence-chain.o \
	 dma-resv.o seqno-fence.o
obj-$(CONFIG_DMABUF_HEAPS)	+= dma-heap.o
obj-$(CONFIG_DMABUF_HEAPS)	+= heaps/
obj-$(CONFIG_SYNC_FILE)		+= sync_file.o
obj-$(CONFIG_SW_SYNC)		+= sw_sync.o sync_debug.o
obj-$(CONFIG_UDMABUF)		+= udmabuf.o

dmabuf_selftests-y := \
	selftest.o \
	st-dma-fence.o \
	st-dma-fence-chain.o

obj-$(CONFIG_DMABUF_SELFTESTS)	+= dmabuf_selftests.o

endif
