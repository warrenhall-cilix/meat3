// SPDX-License-Identifier: GPL-2.0
/*
 * meat3_smu: expose the full RDNA4 (SMU 14.0.2/14.0.3) PMFW metrics table read-only.
 *
 * amdgpu's gpu_metrics sysfs file is built from the firmware's SmuMetricsExternal_t, but only a
 * subset of fields is copied out (no per-rail current, SoC/memory rail voltages, total board power,
 * per-throttler residency, ...). When smu_v14_0_2_get_gpu_metrics() runs it calls
 * smu_cmn_get_metrics_table(smu, &metrics_ext, true), which memcpy()s the whole table into a stack
 * buffer. A kretprobe on that function copies the buffer after it returns.
 *
 * - No firmware messages are sent and no amdgpu structures are dereferenced: the module only reads
 *   the caller's buffer (copy_from_kernel_nofault) after a successful return, and only when the
 *   caller is smu_v14_0_2_get_gpu_metrics, whose buffer is exactly sizeof(SmuMetricsExternal_t).
 * - Data refreshes whenever something reads /sys/class/drm/cardN/device/gpu_metrics (meat3d does,
 *   every poll).
 * - Output: /sys/kernel/debug/meat3_smu/gpuN, one per distinct smu context in first-seen order:
 *   struct { char magic[4] = "M3SM"; u32 version = 1; u32 size; u32 seq; u64 ktime_ns; u8 table[size]; }
 */
#include <linux/module.h>
#include <linux/kprobes.h>
#include <linux/debugfs.h>
#include <linux/spinlock.h>
#include <linux/uaccess.h>
#include <linux/string.h>
#include <linux/timekeeping.h>
#include <linux/ptrace.h>

#define M3_TABLE_SIZE 412	/* sizeof(SmuMetricsExternal_t), smu14_driver_if_v14_0.h (gcc offsetof) */
#define M3_MAX_GPUS 4
#define M3_CALLER "smu_v14_0_2_get_gpu_metrics"

struct m3_hdr {
	char magic[4];
	u32 version;
	u32 size;
	u32 seq;
	u64 ktime_ns;
} __packed;

struct m3_slot {
	void *smu;
	struct m3_hdr hdr;
	u8 table[M3_TABLE_SIZE];
	struct dentry *file;
};

struct m3_call {
	void *smu;
	void *buf;
};

static struct m3_slot slots[M3_MAX_GPUS];
static int nslots;
static DEFINE_SPINLOCK(m3_lock);
static struct dentry *m3_dir;
static unsigned long m3_misses;

static int m3_open(struct inode *inode, struct file *f)
{
	f->private_data = inode->i_private;
	return 0;
}

static ssize_t m3_read(struct file *f, char __user *ubuf, size_t len, loff_t *ppos)
{
	struct m3_slot *s = f->private_data;
	u8 snap[sizeof(struct m3_hdr) + M3_TABLE_SIZE];
	unsigned long flags;

	spin_lock_irqsave(&m3_lock, flags);
	memcpy(snap, &s->hdr, sizeof(s->hdr));
	memcpy(snap + sizeof(s->hdr), s->table, M3_TABLE_SIZE);
	spin_unlock_irqrestore(&m3_lock, flags);
	return simple_read_from_buffer(ubuf, len, ppos, snap, sizeof(snap));
}

static const struct file_operations m3_fops = {
	.owner = THIS_MODULE,
	.open = m3_open,
	.read = m3_read,
	.llseek = default_llseek,
};

static int m3_entry(struct kretprobe_instance *ri, struct pt_regs *regs)
{
	struct m3_call *c = (struct m3_call *)ri->data;

	c->smu = (void *)regs_get_kernel_argument(regs, 0);
	c->buf = (void *)regs_get_kernel_argument(regs, 1);
	return c->buf ? 0 : 1;		/* 1 = don't run the return handler (NULL = cache refresh only) */
}

static int m3_ret(struct kretprobe_instance *ri, struct pt_regs *regs)
{
	struct m3_call *c = (struct m3_call *)ri->data;
	char sym[KSYM_SYMBOL_LEN];
	struct m3_slot *s = NULL;
	u8 tmp[M3_TABLE_SIZE];
	unsigned long flags;
	int i;

	if (regs_return_value(regs) != 0)
		return 0;
	snprintf(sym, sizeof(sym), "%ps", (void *)get_kretprobe_retaddr(ri));
	if (strncmp(sym, M3_CALLER, sizeof(M3_CALLER) - 1) != 0)
		return 0;		/* another ASIC's caller: buffer size unknown, leave it alone */
	if (copy_from_kernel_nofault(tmp, c->buf, M3_TABLE_SIZE)) {
		m3_misses++;
		return 0;
	}
	spin_lock_irqsave(&m3_lock, flags);
	for (i = 0; i < nslots; i++)
		if (slots[i].smu == c->smu)
			s = &slots[i];
	if (!s && nslots < M3_MAX_GPUS) {
		s = &slots[nslots++];
		s->smu = c->smu;
		memcpy(s->hdr.magic, "M3SM", 4);
		s->hdr.version = 1;
		s->hdr.size = M3_TABLE_SIZE;
	}
	if (s) {
		memcpy(s->table, tmp, M3_TABLE_SIZE);
		s->hdr.seq++;
		s->hdr.ktime_ns = ktime_get_ns();
	}
	spin_unlock_irqrestore(&m3_lock, flags);
	return 0;
}

static struct kretprobe m3_probe = {
	.kp.symbol_name = "smu_cmn_get_metrics_table",
	.entry_handler = m3_entry,
	.handler = m3_ret,
	.data_size = sizeof(struct m3_call),
	.maxactive = 8,
};

static ssize_t m3_info_read(struct file *f, char __user *ubuf, size_t len, loff_t *ppos)
{
	char buf[256];
	int n;

	n = scnprintf(buf, sizeof(buf), "slots %d\nnmissed %d\ncopy_faults %lu\ntable_size %d\ncaller %s\n",
		      nslots, m3_probe.nmissed, m3_misses, M3_TABLE_SIZE, M3_CALLER);
	return simple_read_from_buffer(ubuf, len, ppos, buf, n);
}

static const struct file_operations m3_info_fops = {
	.owner = THIS_MODULE,
	.read = m3_info_read,
	.llseek = default_llseek,
};

static int __init m3_init(void)
{
	char name[8];
	int ret, i;

	m3_dir = debugfs_create_dir("meat3_smu", NULL);
	if (IS_ERR(m3_dir))
		return PTR_ERR(m3_dir);
	/* files exist up front; a slot reads as all zeros (seq 0) until its GPU's metrics are read */
	for (i = 0; i < M3_MAX_GPUS; i++) {
		snprintf(name, sizeof(name), "gpu%d", i);
		slots[i].file = debugfs_create_file(name, 0400, m3_dir, &slots[i], &m3_fops);
	}
	debugfs_create_file("info", 0444, m3_dir, NULL, &m3_info_fops);
	ret = register_kretprobe(&m3_probe);
	if (ret) {
		pr_err("meat3_smu: register_kretprobe(smu_cmn_get_metrics_table) failed: %d "
		       "(amdgpu not loaded?)\n", ret);
		debugfs_remove_recursive(m3_dir);
		return ret;
	}
	pr_info("meat3_smu: watching smu_cmn_get_metrics_table for %s\n", M3_CALLER);
	return 0;
}

static void __exit m3_exit(void)
{
	unregister_kretprobe(&m3_probe);
	debugfs_remove_recursive(m3_dir);
}

module_init(m3_init);
module_exit(m3_exit);
MODULE_LICENSE("GPL");
MODULE_AUTHOR("meat3");
MODULE_DESCRIPTION("Read-only full RDNA4 PMFW metrics table for meat3");
MODULE_SOFTDEP("pre: amdgpu");
