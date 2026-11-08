"""Elevated self-test: small offsets, readback, short stress, back to stock."""
import os, sys, time, traceback
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
out = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "selftest_result.txt"), "w", buffering=1)
def p(*a): out.write(" ".join(str(x) for x in a) + "\n")
from headroom.nvml import Nvml, CLK_GRAPHICS, CLK_MEM
from headroom.common import is_admin
from headroom.sensors import Telemetry
from headroom.cuda_stress import Cuda, CoreTest, MemTest
nv = Nvml()
try:
    p("admin", is_admin())
    tele = Telemetry(nv); tele.start()
    cu = Cuda(); ct = CoreTest(cu); ct.calibrate(); ct.golden(); mt = MemTest(cu, fraction=0.5)
    def loaded(label, secs=8):
        clk=[]; mem=[]; pw=[]
        import threading
        st=threading.Event()
        def smp():
            while not st.wait(1):
                g=tele.get().get("gpu",{})
                if g.get("util",0)>=90: clk.append(g["clk"]); mem.append(g["mem_clk"]); pw.append(g["power"])
        th=threading.Thread(target=smp,daemon=True); th.start()
        r=ct.run(secs); m=mt.run(secs/2)
        st.set(); th.join()
        av=lambda l: sum(l)/len(l) if l else 0
        p(label, "core ok=%s rate=%.3e  clk=%.0f mem=%.0f pw=%.0f  bw=%.1f memok=%s" % (r["ok"], r["rate"], av(clk), av(mem), av(pw), m["gbps"], m["ok"]))
    loaded("stock")
    for kind,name in ((CLK_GRAPHICS,"gfx"),(CLK_MEM,"mem")):
        for ps in (0,2): p("offset before", name, "P%d"%ps, nv.get_offset(kind, ps))
    nv.set_offset(CLK_GRAPHICS, 50); nv.set_offset(CLK_MEM, 250)
    for kind,name in ((CLK_GRAPHICS,"gfx"),(CLK_MEM,"mem")):
        for ps in (0,2): p("offset after", name, "P%d"%ps, nv.get_offset(kind, ps))
    p("max clocks now", nv.max_clock(0), nv.max_clock(2))
    loaded("+50/+250")
except Exception:
    p(traceback.format_exc())
finally:
    p("reset errs", nv.reset_all())
    p("offsets final", nv.get_offset(CLK_GRAPHICS,0), nv.get_offset(CLK_MEM,0), nv.get_offset(CLK_MEM,2))
    out.close()
