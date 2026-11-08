import os, sys, time; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from headroom.cuda_stress import Cuda, CoreTest, MemTest
cu = Cuda(); print("SMs", cu.sms)
ct = CoreTest(cu); print("iters", ct.calibrate())
ct.golden(); print("golden ok", list(ct.ref.values())[:1])
r = ct.run(5); print("core", r)
mt = MemTest(cu, fraction=0.5); print("mem GiB", mt.gib)
print("mem", mt.run(5))
mt.free(); cu.close()
