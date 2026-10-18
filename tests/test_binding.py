"""Named browser_exec registry and task-supervisor binding regressions."""
import sys, threading, types, unittest
from contextlib import contextmanager
from unittest.mock import patch
from support import ROOT
from hbh.binding import resolve_task_session, BindingRefused

ENDPOINT='ws://127.0.0.1:9222/devtools/browser/synthetic'

@contextmanager
def named_runtime(*, foreign=False, shared=True, replaced=False, human=False, sidecar=False, profile=False):
    task='task'; base='bu-named-parking' + ('@/tmp/synthetic-profile' if profile else ''); key=base + ('::local' if sidecar else '')
    record={'owner_task_id':base,'session_key':key,'features':{'local':shared}}
    bt=types.ModuleType('tools.browser_tool')
    bt._active_sessions={key:record};bt._cleanup_lock=threading.Lock()
    bt._last_session_key=lambda task:task
    bt._session_info_owned_by_task=lambda r,t,k:r.get('owner_task_id')==t and r.get('session_key')==k
    bt._bare_task_id_for_session_key=lambda k:k.removesuffix('::local')
    session=types.ModuleType('tools.browser_tool_session')
    calls=[]
    def command(task_id, command, args=None, timeout=None):
        calls.append(task_id)
        if replaced:bt._active_sessions[key]=dict(record)
        return {'success':True,'data':{'cdpUrl':ENDPOINT}}
    session._run_browser_command=command
    session._shares_bot_desktop_browser=lambda r:r.get('features',{}).get('local',False)
    def run_fenced(session_info, fn):return fn()
    session.run_fenced=run_fenced
    bu=types.ModuleType('tools.browser_use_cli')
    bu._backend_cache_key=lambda task_id,session_name='',sidecar=False:('bu-named-'+session_name)+('@/tmp/synthetic-profile' if profile else '')+('::local' if sidecar else '')
    bu._exec_route_is_sidecar={base:sidecar}
    supervisor=types.SimpleNamespace(task_id=task,cdp_url=ENDPOINT if not foreign else ENDPOINT+'other',
        _active=True,_state_lock=threading.Lock(),_thread=types.SimpleNamespace(is_alive=lambda:True),
        _loop=types.SimpleNamespace(is_running=lambda:True),_page_session_id='page')
    registry=types.SimpleNamespace(get=lambda t:supervisor if t==task else None)
    bs=types.ModuleType('tools.browser_supervisor');bs.SUPERVISOR_REGISTRY=registry
    tools=types.ModuleType('tools');tools.browser_tool=bt;tools.browser_tool_session=session
    lease=types.SimpleNamespace(get=lambda:types.SimpleNamespace(human=human,epoch=7))
    with patch.dict(sys.modules,{'tools':tools,'tools.browser_tool':bt,'tools.browser_tool_session':session,
                                'tools.browser_use_cli':bu,'tools.browser_supervisor':bs}):
        yield lease,calls,supervisor

class NamedBindingTests(unittest.TestCase):
    def test_named_session_owned_via_matching_task_supervisor(self):
        with named_runtime() as (lease,calls,_):
            b=resolve_task_session('task',lease=lease,browser_session='parking')
            self.assertEqual((b.session_key,b.cdp_endpoint,b.lease_epoch),('bu-named-parking',ENDPOINT,7))
            self.assertEqual(calls,['bu-named-parking'])
    def test_gateway_profile_scoped_key_is_accepted(self):
        with named_runtime(profile=True) as (lease,calls,_):
            b=resolve_task_session('task',lease=lease,browser_session='parking')
            self.assertEqual(b.session_key,'bu-named-parking@/tmp/synthetic-profile')
            self.assertEqual(calls,[b.session_key])

    def test_foreign_supervisor_endpoint_is_refused(self):
        with named_runtime(foreign=True) as (lease,_,__):
            with self.assertRaisesRegex(BindingRefused,'supervision'):
                resolve_task_session('task',lease=lease,browser_session='parking')
    def test_cloud_browser_is_refused_before_query(self):
        with named_runtime(shared=False) as (lease,calls,_):
            with self.assertRaises(BindingRefused):resolve_task_session('task',lease=lease,browser_session='parking')
            self.assertEqual(calls,[])
    def test_replaced_named_session_is_refused(self):
        with named_runtime(replaced=True) as (lease,_,__):
            with self.assertRaisesRegex(BindingRefused,'changed'):
                resolve_task_session('task',lease=lease,browser_session='parking')
    def test_human_hold_blocks_named_endpoint_query(self):
        with named_runtime(human=True) as (lease,calls,_):
            with self.assertRaisesRegex(BindingRefused,'human'):
                resolve_task_session('task',lease=lease,browser_session='parking')
            self.assertEqual(calls,[])
    def test_named_sidecar_uses_exact_route(self):
        with named_runtime(sidecar=True) as (lease,calls,_):
            b=resolve_task_session('task',lease=lease,browser_session='parking')
            self.assertEqual(b.session_key,'bu-named-parking::local')
    def test_stopped_or_foreign_task_supervisor_refuses(self):
        with named_runtime() as (lease,calls,s):
            s._active=False
            with self.assertRaises(BindingRefused):resolve_task_session('task',lease=lease,browser_session='parking')
            self.assertEqual(calls,[])

class ToolWiringTests(unittest.TestCase):
    def test_public_tool_forwards_named_session_and_target(self):
        import hbh
        from unittest.mock import Mock
        handlers = {}
        class Context:
            def on_unload(self, hook): pass
            def register_tool(self, **kw): handlers[kw['name']] = kw
            def register_system_prompt_section(self, **kw): pass
        hbh.register(Context())
        manager = types.SimpleNamespace(create=Mock(return_value={'success':True}))
        service = types.SimpleNamespace(call=lambda fn:fn(manager))
        tool = handlers['homelab_browser_handover']
        self.assertIn('browser_session', tool['schema']['parameters']['properties'])
        with patch.object(hbh, '_get_service', return_value=service):
            tool['handler']({'page_origin':'https://shop.example','purpose':'Test','target_id':'TARGET',
                             'browser_session':'parking'}, task_id='task')
        manager.create.assert_called_once_with('task','https://shop.example','Test','TARGET','parking')
