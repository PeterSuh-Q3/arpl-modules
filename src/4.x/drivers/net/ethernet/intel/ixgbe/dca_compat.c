// src/4.x/drivers/net/ethernet/intel/ixgbe/dca_compat.c
#include <linux/module.h>
#include <linux/dca.h>

extern u8 dca_get_tag(int cpu);

u8 dca3_get_tag(struct device *dev, int cpu)
{
    return dca_get_tag(cpu);
}
EXPORT_SYMBOL_GPL(dca3_get_tag);

static int __init dca_compat_init(void)
{
    pr_info("DCA v3 compatibility shim loaded\n");
    return 0;
}

static void __exit dca_compat_exit(void)
{
    pr_info("DCA v3 compatibility shim unloaded\n");
}

module_init(dca_compat_init);
module_exit(dca_compat_exit);

MODULE_LICENSE("GPL");
MODULE_AUTHOR("Xpenology User");
MODULE_DESCRIPTION("DCA v3 API compatibility for ixgbe");
