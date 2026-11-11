import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('setup_script', ROOT/'scripts/setup.py')
setup=importlib.util.module_from_spec(spec);spec.loader.exec_module(setup)

class SetupTests(unittest.TestCase):
    def arguments(self):
        return setup.parser().parse_args(['--non-interactive','--server-ip','127.0.0.1',
            '--allow','127.0.0.1/32','--home-lat','40','--home-lon','-100','--timezone','UTC'])

    def test_generated_configuration_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'firmware/main').mkdir(parents=True)
            old_mask=os.umask(0o077)
            try: setup.provision(self.arguments(),root)
            finally: os.umask(old_mask)
            data=json.loads((root/'secrets/auth.json').read_text())
            token=(root/'secrets/badge-1.token').read_text().strip()
            from hashlib import sha256
            self.assertEqual(data['devices']['badge-1'],sha256(token.encode()).hexdigest())
            self.assertNotIn(token,(root/'.env').read_text())
            self.assertNotIn(token,json.dumps(data))
            self.assertEqual((root/'generated/allowed-clients.conf').read_text(),'allow 127.0.0.1/32;\n')
            subprocess.run(['openssl','verify','-CAfile',str(root/'generated/dashboard-ca.crt'),
                            '-verify_ip','127.0.0.1',str(root/'secrets/tls/server.crt')],check=True,stdout=subprocess.DEVNULL)
            self.assertEqual((root/'secrets/badge-1.token').stat().st_mode & 0o777,0o600)
            with self.assertRaises(ValueError): setup.provision(self.arguments(),root)

    def test_invalid_inputs_create_no_secrets(self):
        for field,value in [('server_ip','not-an-ip'),('allow',['0.0.0.0/0;evil']),
                            ('home_lat',100),('work_lat',1),('timezone','Not/AZone'),('label',"bad'label")]:
            args=self.arguments();setattr(args,field,value)
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises((ValueError,KeyError)): setup.provision(args,Path(tmp))
                self.assertFalse((Path(tmp)/'secrets').exists())

class ConfigurationTests(unittest.TestCase):
    def test_disabled_traffic_does_not_read_key_or_fetch(self):
        import dashboard as app
        with patch.object(app,'WORK',None),patch.object(app,'fetch') as fetch:
            self.assertEqual(app.fetch_commute(),{'status':'disabled'})
            fetch.assert_not_called()

    def test_live_startup_requires_location(self):
        import dashboard as app
        with patch.dict(os.environ,{},clear=True):
            with self.assertRaises(ValueError): app.validate_configuration()
