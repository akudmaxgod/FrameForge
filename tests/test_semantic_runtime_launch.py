import os,subprocess,sys,tempfile,unittest
from pathlib import Path

class SemanticRuntimeLaunchTests(unittest.TestCase):
    def test_direct_worker_exposes_its_own_package_without_pythonpath(self):
        root=Path(__file__).resolve().parents[1];script=root/'imagegen/semantic_runtime.py'
        env=dict(os.environ);env.pop('PYTHONPATH',None)
        probe="import runpy,sys;runpy.run_path(sys.argv[1]);import imagegen.gpu_runtime;print(imagegen.gpu_runtime.__file__)"
        with tempfile.TemporaryDirectory() as cwd:
            result=subprocess.run([sys.executable,'-I','-c',probe,str(script)],cwd=cwd,env=env,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(Path(result.stdout.strip()).resolve(),(root/'imagegen/gpu_runtime.py').resolve())
