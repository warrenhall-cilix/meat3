"""Read-only cross-check of PM-table fields against independent sources (run as root)."""
import time
from meat3.backends.cpu_amd import AmdCpuBackend

b = AmdCpuBackend.detect()[0]
b.sensors()
pairs = [("pmt.ppt", "rapl.intel_rapl_0"), ("pmt.ppt", "msr.pkg_power"), ("pmt.socket_power", "rapl.intel_rapl_0"),
         ("pmt.thm", "k10temp.temp1"), ("pmt.mclk", "mem.umc_clk"), ("pmt.tdc", "pmt.vddcr_cpu_cur"),
         ("pmt.vddcr_cpu_power", "rapl.intel_rapl_0_0"), ("pmt.vddcr_cpu_power", "msr.cores_power")]
for k in range(8):
    pairs += [(f"pmt.core{k}.power", f"core{k}.power"), (f"pmt.core{k}.c0", f"core{k}.c0"),
              (f"pmt.core{k}.cur", f"core{k}.power")]
rows = {p: [] for p in pairs}
for i in range(6):
    time.sleep(1.0)
    s = {x.id[5:]: x.value for x in b.sensors()}
    for a, c in pairs:
        rows[(a, c)].append((s.get(a), s.get(c)))
for (a, c), v in rows.items():
    print(f"{a:24s} vs {c:22s} " + "  ".join("%7.2f/%-7.2f" % (x or -1, y or -1) for x, y in v))
