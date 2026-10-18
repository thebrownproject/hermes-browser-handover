"""Optional WebKit acceptance. Requires PLAYWRIGHT_CORE and its WebKit install.

All browser profiles, target pages, identities and control leases are synthetic.
Touch events are injected into WebKit's DOM; physical iPhone testing remains separate.
"""
import json,os,subprocess,unittest
from pathlib import Path
from support import Env

@unittest.skipUnless(os.environ.get('PLAYWRIGHT_CORE'), 'Set PLAYWRIGHT_CORE to the installed playwright-core package')
class WebKitUI(unittest.TestCase):
 def test_native_touch_scroll_light_dark(self):
  env=Env()
  try:
   result=env.create()
   process_env=dict(os.environ,HBH_WEBKIT_FIXTURE=json.dumps({'origin':env.public_origin,'handover_id':result['handover_id'],'cdp':env.ws_url}))
   done=subprocess.run(['node',str(Path(__file__).with_name('webkit_scroll.cjs'))],env=process_env,capture_output=True,text=True,timeout=90)
   self.assertEqual(done.returncode,0,done.stdout+'\n'+done.stderr)
   report=json.loads(done.stdout.strip().splitlines()[-1]);self.assertTrue(report['finish']);self.assertEqual(len(report['results']),2)
   print(json.dumps(report))
  finally:env.close()
