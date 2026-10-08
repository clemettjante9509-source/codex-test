import base64
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import server
try:
 from web import create_app
except ImportError:
 create_app=None

@unittest.skipIf(create_app is None,'在线部署测试需先安装 requirements.txt')
class WebTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.old_db=server.DB;server.DB=Path(self.temp.name)/'web.sqlite'
  self.config={'TESTING':True,'APP_PASSWORD':'test-password-at-least-12','ALLOWED_HOSTS':['localhost'],'REQUIRE_HTTPS':True}
  self.app=create_app(self.config);self.client=self.app.test_client()
  self.headers={'Authorization':'Basic '+base64.b64encode(b'homework:test-password-at-least-12').decode()}
 def tearDown(self):server.DB=self.old_db;self.temp.cleanup()
 def get(self,path,**kwargs):return self.client.get(path,base_url='https://localhost',headers=self.headers,**kwargs)
 def post(self,path,data):return self.client.post(path,json=data,base_url='https://localhost',headers=self.headers)
 def test_default_refuses_missing_password(self):
  with patch.dict('os.environ',{'APP_PASSWORD':''}),self.assertRaises(RuntimeError):create_app()
 def test_health_only_is_public(self):
  self.assertEqual(self.client.get('/health').status_code,200)
  for path in ('/','/api/errors','/api/example','/api/export','/api/config'):
   self.assertEqual(self.client.get(path,base_url='https://localhost').status_code,401)
  self.assertEqual(self.client.post('/api/grade',base_url='https://localhost',json={}).status_code,401)
 def test_wrong_password_rejected(self):
  h={'Authorization':'Basic '+base64.b64encode(b'homework:wrong').decode()}
  self.assertEqual(self.client.get('/',base_url='https://localhost',headers=h).status_code,401)
 def test_host_https_and_origin(self):
  self.assertEqual(self.client.get('/',base_url='https://evil.example',headers=self.headers).status_code,403)
  self.assertEqual(self.client.get('/',headers=self.headers).status_code,400)
  h={**self.headers,'Origin':'https://evil.example'}
  self.assertEqual(self.client.post('/api/grade',base_url='https://localhost',headers=h,json={}).status_code,403)
 def test_proxy_https_and_origin(self):
  h={**self.headers,'X-Forwarded-Proto':'https','Origin':'https://localhost'}
  r=self.client.post('/api/ocr',headers=h,json={})
  self.assertEqual(r.status_code,501)
 def test_historical_workflow_and_restart(self):
  d=self.get('/api/example').json;d['answer']='奖励耕织'
  r=self.post('/api/grade',d);self.assertEqual(r.status_code,200);v=r.json
  self.assertEqual(v['result']['score'],2)
  compact=self.post('/api/report',{'id':v['id'],'compact':True});self.assertIn('⑩',compact.json['report'])
  confirmed=self.post('/api/confirm',{'id':v['id'],'earned':[2,0],'verdict':'部分得分','error_type':'C'});self.assertEqual(confirmed.status_code,200)
  self.assertEqual(len(self.get('/api/errors?error_type=C&status=confirmed').json),1)
  self.assertIn('军功',self.get('/api/export?error_type=C').data.decode())
  self.client=create_app(self.config).test_client()
  self.assertEqual(len(self.get('/api/errors').json),1)
 def test_security_headers_and_static(self):
  r=self.get('/');self.assertIn('初一',r.data.decode());self.assertEqual(r.headers['X-Frame-Options'],'DENY');self.assertIn("frame-ancestors 'none'",r.headers['Content-Security-Policy'])
  js=self.get('/app.js');self.assertEqual(js.status_code,200);js.close();r.close()
 def test_oversized_body_rejected(self):
  r=self.post('/api/grade',{'question':'x'*100001});self.assertEqual(r.status_code,413)
 def test_rate_limit(self):
  for _ in range(30):self.assertEqual(self.post('/api/ocr',{}).status_code,501)
  self.assertEqual(self.post('/api/ocr',{}).status_code,429)
 def test_storage_notice_and_no_model_secret_exposure(self):
  with patch.dict('os.environ',{'STORAGE_PERSISTENT':'false','GRADING_API_KEY':'mock-secret-not-real'}):
   r=self.get('/api/config');self.assertFalse(r.json['storage_persistent']);self.assertTrue(r.json['model_available']);self.assertNotIn('mock-secret-not-real',r.data.decode())
 def test_invalid_confirmation_not_stored(self):
  d=self.get('/api/example').json;d['answer']='奖励耕织'
  v=self.post('/api/grade',d).json
  r=self.post('/api/confirm',{'id':v['id'],'verdict':'正确','earned':[2,0]});self.assertEqual(r.status_code,400)
  self.assertFalse(self.get('/api/errors').json[0]['confirmed'])

if __name__=='__main__':unittest.main()
