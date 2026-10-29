import contextlib
import io
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
        answers=['192.0.2.11,192.0.2.12','10.0.0.11,10.0.0.12','edge.example.org','127.0.0.1',values['PROBE_TARGETS'],'','','']
        with patch.object(install,'ask',side_effect=answers):install.configure_controller(values)
        self.assertEqual(len(values['ADMIN_TOKEN']),64)
        self.assertNotIn('REPORT_SECRET',values);self.assertNotIn('CONTROLLER_REPORT_URL',values)
        self.assertEqual(values['DRY_RUN'],'true');self.assertEqual(values['ENABLE_DNS_WRITES'],'')
    def test_setup_persists_config_before_data_init_and_never_creates_pairing(self):
        with patch.object(install,'ask',return_value=''),patch.object(install,'configure_controller'), \
             patch.object(install,'prepare_image',return_value='image'),patch.object(install,'run',return_value=''), \
             patch.object(install,'initialize_data',side_effect=RuntimeError('failed')):
            with self.assertRaises(RuntimeError):install.setup('controller')
        self.assertTrue((self.root/'deploy/controller/.env').exists())
        self.assertFalse((self.root/'deploy/controller/monitor-pairing.json').exists())
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
