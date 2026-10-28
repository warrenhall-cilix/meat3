obj-m := meat3_smu.o
KDIR ?= /lib/modules/$(shell uname -r)/build
# kernels built with clang (CachyOS, ...) need modules built with LLVM too
ifneq ($(shell grep -s '^CONFIG_CC_IS_CLANG=y' $(KDIR)/.config),)
export LLVM ?= 1
endif

all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
