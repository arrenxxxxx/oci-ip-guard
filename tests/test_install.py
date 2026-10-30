import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from guard import install

class InstallTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        shutil.copytree(install.ROOT/'deploy',self.root/'deploy',ignore=shutil.ignore_patterns('data','.env'))
        self.root_patch=patch.object(install,'ROOT',self.root);self.root_patch.start()
        self.output=io.StringIO();self.redirect=contextlib.redirect_stdout(self.output);self.redirect.__enter__()
    def tearDown(self):
        self.redirect.__exit__(None,None,None);self.root_patch.stop();self.temp.cleanup()
    def test_private_files_exclusive_and_literal(self):
        path=self.root/'.env';values={'ADMIN_TOKEN':'literal$not-expanded#value','DRY_RUN':'true'}
        install.write_private(path,install.env_text(values))
        self.assertEqual(path.stat().st_mode & 0o777,0o600);self.assertEqual(install.read_env(path),values)
        with self.assertRaises(FileExistsError):install.write_private(path,'overwrite')
    def test_existing_config_preserved(self):
        path=self.root/'deploy/controller/.env';path.write_text('DRY_RUN=false\n')
        with patch.object(install,'run') as run:
            with self.assertRaises(ValueError):install.setup('controller')
        run.assert_not_called();self.assertEqual(path.read_text(),'DRY_RUN=false\n')
    def test_private_listener_and_pinned_image(self):
        for ip in ['0.0.0.0','1.1.1.1']:
            with self.assertRaises(ValueError):install.private_bind(ip)
        for image in ['ghcr.io/org/name:latest','ghcr.io/org/name','$(command)']:
            with self.assertRaises(ValueError):install.image_ref(image,'controller')
    def test_cloudflare_discovery_get_only(self):
        values=dict(CF_API_TOKEN='secret',CF_ZONE_ID='a'*32,DNS_NAME='edge.example.org',ENTRY_IPS='192.0.2.11,192.0.2.12')
        record=dict(id='record',name='edge.example.org',type='A',proxied=False,content='192.0.2.11')
        with patch.object(install,'request',return_value={'success':True,'result':[record]}) as api:
            self.assertEqual(install.cloudflare_record(values),'record');self.assertEqual(len(api.call_args.args),1)
        record['proxied']=True
        with patch.object(install,'request',return_value={'success':True,'result':[record]}):
            with self.assertRaises(ValueError):install.cloudflare_record(values)
    def test_single_machine_prompts_no_pairing_or_remote_url(self):
        values=install.read_env(self.root/'deploy/controller/.env.example')
        answers=['2','192.0.2.11,192.0.2.12','10.0.0.11,10.0.0.12','edge.example.org','127.0.0.1',values['PROBE_TARGETS'],'','','']
        with patch.object(install,'ask',side_effect=answers):install.configure_controller(values)
        self.assertEqual(len(values['ADMIN_TOKEN']),64)
        self.assertNotIn('REPORT_SECRET',values);self.assertNotIn('CONTROLLER_REPORT_URL',values)
        self.assertEqual(values['DRY_RUN'],'true');self.assertEqual(values['ENABLE_DNS_WRITES'],'')
    def test_setup_persists_config_before_data_init_and_never_creates_pairing(self):
        with patch.object(install,'ask',return_value=''),patch.object(install,'configure_controller',side_effect=lambda v:v.update(RUN_MODE='simulate')), \
             patch.object(install,'prepare_image',return_value='image'),patch.object(install,'run',return_value=''), \
             patch.object(install,'initialize_data',side_effect=RuntimeError('failed')):
            with self.assertRaises(RuntimeError):install.setup('controller')
        self.assertTrue((self.root/'deploy/controller/.env').exists())
        self.assertFalse((self.root/'deploy/controller/monitor-pairing.json').exists())
    def test_explicit_mode_required(self):
        for answer in ('', 'yes', '0', 'dry run'):
            with self.subTest(answer=answer), patch.object(install, 'ask', return_value=answer):
                with self.assertRaisesRegex(ValueError, '未明确选择'):
                    install.choose_mode()
        for answer, mode in [('1', 'live'), ('2', 'simulate')]:
            with patch.object(install, 'ask', return_value=answer):
                self.assertEqual(install.choose_mode(), mode)

    def test_live_requires_credentials_before_consent(self):
        values=install.read_env(self.root/'deploy/controller/.env.example')
        answers=['1','192.0.2.11,192.0.2.12','10.0.0.11,10.0.0.12','edge.example.org',
                 '127.0.0.1',values['PROBE_TARGETS'],'']
        with patch.object(install, 'ask', side_effect=answers):
            with self.assertRaisesRegex(ValueError, 'Cloudflare Token'):
                install.configure_controller(values)
        self.assertEqual(values['RUN_MODE'], 'live')
        self.assertEqual(values['ENABLE_DNS_WRITES'], '')

    def test_live_confirmation_requires_exact_phrase(self):
        values={'DNS_NAME':'edge.example.org','ENTRY_IPS':'192.0.2.11,192.0.2.12',
                'ENABLE_DNS_WRITES':''}
        for answer in ('', 'yes', 'enable dns writes'):
            with patch.object(install, 'ask', return_value=answer):
                with self.assertRaisesRegex(ValueError, '未确认 DNS 写入'):
                    install.confirm_live(values)
            self.assertEqual(values['ENABLE_DNS_WRITES'], '')
        with patch.object(install, 'ask', return_value='ENABLE DNS WRITES'):
            install.confirm_live(values)
        self.assertEqual(values['ENABLE_DNS_WRITES'], 'I_HAVE_REVIEWED_DRY_RUN')

    def test_preflight_uses_private_literal_env_and_removes_it(self):
        values={'CF_API_TOKEN':'secret$literal#value','RUN_MODE':'live','ENABLE_DNS_WRITES':''}
        paths=[]
        def invoke(args, **kwargs):
            path=Path(args[args.index('--env-file')+1]);paths.append(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIn('CF_API_TOKEN=secret$literal#value\n', path.read_text())
            self.assertNotIn(values['CF_API_TOKEN'], ' '.join(args))
            self.assertEqual(args[-3:], ['python','-m','guard.preflight'])
            self.assertIn('--read-only', args)
            return type('Result', (), {'stdout':'{"ok":true,"reasons":[]}', 'returncode':0})()
        with patch.object(install.subprocess, 'run', side_effect=invoke):
            install.preflight_live(values, 'image')
        self.assertFalse(paths[0].exists())
        self.assertEqual(values['ENABLE_DNS_WRITES'], '')

    def test_preflight_failure_reports_safe_reason(self):
        with patch.object(install.subprocess, 'run') as run:
            run.return_value.returncode=1
            run.return_value.stdout=json.dumps({'ok':False,'reasons':['SOURCE_IPS mapping invalid; correct local addresses']})
            run.return_value.stderr='secret-token'
            with self.assertRaisesRegex(ValueError, 'SOURCE_IPS mapping invalid') as error:
                install.preflight_live({'RUN_MODE':'live'}, 'image')
            self.assertNotIn('secret-token', str(error.exception))

    def test_failed_preflight_never_confirms_or_persists_config(self):
        with patch.object(install,'ask',return_value=''), \
             patch.object(install,'configure_controller',side_effect=lambda v:v.update(RUN_MODE='live')), \
             patch.object(install,'prepare_image',return_value='image'),patch.object(install,'run',return_value=''), \
             patch.object(install,'preflight_live',side_effect=ValueError('blocked')), \
             patch.object(install,'confirm_live') as confirm,patch.object(install,'initialize_data') as initialize:
            with self.assertRaises(ValueError):install.setup('controller')
        confirm.assert_not_called();initialize.assert_not_called()
        self.assertFalse((self.root/'deploy/controller/.env').exists())

    def test_cancelled_confirmation_never_persists_live_config(self):
        with patch.object(install,'ask',return_value=''), \
             patch.object(install,'configure_controller',side_effect=lambda v:v.update(RUN_MODE='live',DNS_NAME='edge.example.org')), \
             patch.object(install,'prepare_image',return_value='image'),patch.object(install,'run',return_value=''), \
             patch.object(install,'preflight_live') as preflight, patch.object(install,'initialize_data') as initialize:
            with self.assertRaisesRegex(ValueError, '未确认 DNS 写入'):install.setup('controller')
        preflight.assert_called_once();initialize.assert_not_called()
        self.assertFalse((self.root/'deploy/controller/.env').exists())

    def test_live_setup_preflight_then_consent_then_persist(self):
        order=[]
        def configure(v):
            v.update(RUN_MODE='live',DRY_RUN='false',DNS_NAME='edge.example.org',ENABLE_DNS_WRITES='')
        def confirm(v):
            order.append('confirm')
            self.assertFalse((self.root/'deploy/controller/.env').exists())
            v['ENABLE_DNS_WRITES']='I_HAVE_REVIEWED_DRY_RUN'
        def initialize(*args):
            order.append('initialize')
            self.assertEqual(install.read_env(self.root/'deploy/controller/.env')['ENABLE_DNS_WRITES'],
                             'I_HAVE_REVIEWED_DRY_RUN')
        with patch.object(install,'ask',return_value=''),patch.object(install,'configure_controller',side_effect=configure), \
             patch.object(install,'prepare_image',return_value='image'),patch.object(install,'run',return_value=''), \
             patch.object(install,'preflight_live',side_effect=lambda *args:order.append('preflight')), \
             patch.object(install,'confirm_live',side_effect=confirm),patch.object(install,'initialize_data',side_effect=initialize):
            install.setup('controller')
        self.assertEqual(order, ['preflight','confirm','initialize'])

    def test_compose_environment_does_not_override_saved_config(self):
        path=self.root/'deploy/controller/.env';path.write_text("IP_GUARD_IMAGE='safe:v1'\n")
        with patch.dict(os.environ,{'IP_GUARD_IMAGE':'wrong:v1','COMPOSE_PROJECT_NAME':'other'}),patch.object(install.subprocess,'run') as process:
            process.return_value.returncode=0;process.return_value.stdout=''
            install.run(install.compose('controller')+['config','--quiet'])
        self.assertNotIn('IP_GUARD_IMAGE',process.call_args.kwargs['env']);self.assertNotIn('COMPOSE_PROJECT_NAME',process.call_args.kwargs['env'])
    def test_existing_docker_project_preserved(self):
        with patch.object(install,'run',return_value='container-id'),patch.object(install,'prepare_image') as prepare:
            with self.assertRaises(ValueError):install.setup('controller')
        prepare.assert_not_called()
    def test_error_does_not_print_secret(self):
        with patch.object(install.subprocess,'run') as p:
            p.return_value.returncode=1;p.return_value.stderr='private-token'
            with self.assertRaises(RuntimeError) as e:install.run(['docker','compose','up'])
        self.assertNotIn('private-token',str(e.exception))

if __name__=='__main__':unittest.main()
