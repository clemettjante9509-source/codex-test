import base64
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
try:
 from PIL import Image
 from vision import photo_grade,validate_image
 from web import create_app
except ImportError:
 Image=None
import engine
import server

@unittest.skipIf(Image is None,'拍照测试需先安装requirements.txt')
class PhotoTests(unittest.TestCase):
 def setUp(self):
  buf=io.BytesIO();Image.new('RGB',(100,100),'white').save(buf,'PNG')
  self.image='data:image/png;base64,'+base64.b64encode(buf.getvalue()).decode()
  self.data={'subject':'历史','image':self.image}
 def response(self,items,truncated=False):
  class Response:
   def __enter__(self):return self
   def __exit__(self,*args):pass
   def read(self,*args):return json.dumps({'choices':[{'message':{'content':json.dumps({'items':items,'truncated':truncated},ensure_ascii=False)}}]}).encode()
  return Response()
 def item(self):
  d={**engine.EXAMPLE,'answer':'奖励耕织'};r=engine.grade(d)
  return {'number':'1','question':d['question'],'answer':d['answer'],'legible':True,'issue':'','grading':r}
 def test_image_validation(self):
  self.assertEqual(validate_image(self.image),self.image)
  for value in ('data:image/png;base64,not-image','data:image/svg+xml;base64,PHN2Zz4=','https://example.com/a.png',self.image.replace('image/png','image/jpeg')):
   with self.assertRaises(ValueError):validate_image(value)
 def test_missing_key(self):
  with patch.dict('os.environ',{},clear=True),self.assertRaisesRegex(ValueError,'尚未启用'):photo_grade(self.data)
 def test_real_image_payload_and_grade(self):
  with patch.dict('os.environ',{'GRADING_API_KEY':'test-only'}),patch('vision.urllib.request.urlopen',return_value=self.response([self.item()])) as mock:
   result=photo_grade(self.data)
  payload=json.loads(mock.call_args.args[0].data)
  self.assertEqual(payload['messages'][1]['content'][1]['image_url']['url'],self.image)
  self.assertEqual(result['items'][0]['grading']['score'],2)
  self.assertTrue(result['items'][0]['grading']['needs_review'])
 def test_unclear_questions_have_no_grade(self):
  item=self.item();item.update(legible=False,issue='学生字迹不清')
  with patch.dict('os.environ',{'GRADING_API_KEY':'test-only'}),patch('vision.urllib.request.urlopen',return_value=self.response([item])):
   self.assertIsNone(photo_grade(self.data)['items'][0]['grading'])
 def test_invalid_scores_fail_without_saving(self):
  item=self.item();item['grading']['score']=4
  with patch.dict('os.environ',{'GRADING_API_KEY':'test-only'}),patch('vision.urllib.request.urlopen',return_value=self.response([item])),self.assertRaises(ValueError):photo_grade(self.data)
 def test_limit_and_empty(self):
  for items in ([],[self.item()]*9):
   with patch.dict('os.environ',{'GRADING_API_KEY':'test-only'}),patch('vision.urllib.request.urlopen',return_value=self.response(items)),self.assertRaises(ValueError):photo_grade(self.data)
 def test_photo_api_records_only_legible_items(self):
  with tempfile.TemporaryDirectory() as directory,patch.object(server,'DB',Path(directory)/'db.sqlite'):
   app=create_app({'TESTING':True,'APP_PASSWORD':'photo-test-password-only','ALLOWED_HOSTS':['localhost'],'REQUIRE_HTTPS':False})
   client=app.test_client();headers={'Authorization':'Basic '+base64.b64encode(b'homework:photo-test-password-only').decode()}
   unclear=self.item();unclear.update(legible=False,issue='题目被裁掉')
   with patch.dict('os.environ',{'GRADING_API_KEY':'test-only'}),patch('vision.urllib.request.urlopen',return_value=self.response([self.item(),unclear],True)):
    response=client.post('/api/photo-grade',json=self.data,headers=headers)
   self.assertEqual(response.status_code,200);self.assertTrue(response.json['truncated'])
   self.assertIn('id',response.json['items'][0]);self.assertIsNone(response.json['items'][1]['grading'])
   self.assertEqual(len(client.get('/api/errors',headers=headers).json),1)
 def test_photo_api_requires_password(self):
  app=create_app({'TESTING':True,'APP_PASSWORD':'photo-test-password-only','ALLOWED_HOSTS':['localhost'],'REQUIRE_HTTPS':False})
  self.assertEqual(app.test_client().post('/api/photo-grade',json=self.data).status_code,401)
