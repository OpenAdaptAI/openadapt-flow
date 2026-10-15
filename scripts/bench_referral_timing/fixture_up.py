import os, sys, time
sys.argv = ["x"]; sys.path.insert(0, "/Users/abrichr/oa/scratch/benchmark/harness")
import bench
bench.do_imports(); f = bench.G["Fixture"]()
t = time.time(); f._compose("up", "-d", timeout_s=600.0); f._wait_ready(); f._verify_running_source()
print("up", round(time.time() - t, 1)); t = time.time(); print("reset", f.reset()[:12], round(time.time() - t, 1))
print("patients", bench.sql_patients(f, "1=1"))
print("token", f.token_session("oracle") is not None)
