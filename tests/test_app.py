import copy
import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import patch
import engine
import server

class EngineTests(unittest.TestCase):
 def example(self):
  d=copy.deepcopy(engine.EXAMPLE);d['answer']='奖励耕织，促进农业。';return d
 def test_history_points_and_template(self):
  r=engine.grade(self.example())
  self.assertEqual((r['score'],r['total'],r['verdict']),(2,4,'部分得分'))
  self.assertTrue(r['needs_review']);self.assertEqual(r['error_type'],'')
  text=engine.report(r)
  for label in '①②③④⑤⑥⑦⑧⑨⑩':self.assertEqual(text.count(label),1)
  self.assertIn(engine.FRAME,text);self.assertIn('奖励军功',text)
 def test_negation_never_finalized(self):
  d=self.example();d['answer']='不应该奖励耕织，也不应该奖励军功。'
  self.assertTrue(engine.grade(d)['needs_review'])
 def test_all_subjects_and_no_guesses(self):
  for subject in engine.SUBJECTS:
   d={'subject':subject,'question':'测试题','answer':'测试答案'}
   with self.assertRaises(ValueError):engine.grade(d)
   d.update(points=[{'text':'测试答案','keywords':['测试答案'],'score':2}])
   self.assertEqual(engine.grade(d)['score'],2)
 def test_exact(self):
  d={'subject':'英语','question':'写出苹果的英语单词','answer':'Apple.','reference':'apple','mode':'exact'}
  self.assertEqual(engine.grade(d)['verdict'],'正确')
  d['answer']='banana';r=engine.grade(d);self.assertEqual(r['verdict'],'错误');self.assertFalse(r['needs_review'])
 def test_validation(self):
  d=self.example();d['points'][0]['score']=-1
  with self.assertRaises(ValueError):engine.grade(d)
  d=self.example();d['variant']={'question':'缺少答案'}
  with self.assertRaises(ValueError):engine.grade(d)
 def test_model_missing_key(self):
  d=self.example();d['mode']='model'
  with patch.dict('os.environ',{},clear=True),self.assertRaises(ValueError):engine.grade(d)
 def test_model_response_and_invalid_evidence(self):
  d=self.example();valid=engine.grade(d);valid['points'][0]['evidence']='奖励耕织';valid['points'][1]['evidence']=''
  class Response:
   def __init__(self,payload):self.payload=payload
   def __enter__(self):return self
   def __exit__(self,*args):pass
   def read(self,*args):return json.dumps({'choices':[{'message':{'content':json.dumps(self.payload,ensure_ascii=False)}}]}).encode()
  d['mode']='model'
  with patch.dict('os.environ',{'GRADING_API_KEY':'test-only-not-a-real-key'}):
   with patch('engine.urllib.request.urlopen',return_value=Response(valid)):
    r=engine.grade(d);self.assertTrue(r['needs_review']);self.assertEqual(r['score'],2)
   invalid=copy.deepcopy(valid);invalid['score']=4
   with patch('engine.urllib.request.urlopen',return_value=Response(invalid)),self.assertRaises(ValueError):engine.grade(d)
   invalid=copy.deepcopy(valid);invalid['points'][0]['evidence']='学生没有说过的句子'
   with patch('engine.urllib.request.urlopen',return_value=Response(invalid)),self.assertRaises(ValueError):engine.grade(d)
 def test_merge_report_keeps_template(self):
  text=engine.report(engine.grade(self.example()),True,True)
  self.assertIn('沿用上一题',text)
  for label in '①②③④⑤⑥⑦⑧⑨⑩':self.assertIn(label,text)

class HTTPTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.temp=tempfile.TemporaryDirectory();server.DB=Path(cls.temp.name)/'db.sqlite'
  cls.http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
  cls.thread=threading.Thread(target=cls.http.serve_forever,daemon=True);cls.thread.start()
  cls.base='http://127.0.0.1:'+str(cls.http.server_port)
 @classmethod
 def tearDownClass(cls):cls.http.shutdown();cls.http.server_close();cls.temp.cleanup()
 def request(self,path,data=None):
  req=urllib.request.Request(self.base+path,data=json.dumps(data).encode() if data else None,headers={'Content-Type':'application/json'})
  with urllib.request.urlopen(req) as r:return json.load(r)
 def test_end_to_end_confirm_filter_export_merge(self):
  d=copy.deepcopy(engine.EXAMPLE);d['answer']='奖励耕织'
  first=self.request('/api/grade',d);second=self.request('/api/grade',d)
  self.assertTrue(second['merged'])
  rows=self.request('/api/errors');self.assertEqual(rows[0]['group_id'],rows[1]['group_id'])
  confirmed=self.request('/api/confirm',{'id':first['id'],'verdict':'部分得分','earned':[2,0],'error_type':'C'})
  self.assertFalse(confirmed['result']['needs_review'])
  rows=self.request('/api/errors?subject='+urllib.parse.quote('历史')+'&error_type=C&status=confirmed');self.assertEqual(len(rows),1)
  with urllib.request.urlopen(self.base+'/api/export?error_type=C') as response:
   text=response.read().decode();self.assertIn('商鞅变法',text);self.assertIn('变式',text)
  d['answer']=engine.EXAMPLE['answer'];self.request('/api/grade',d)
  d['answer']='奖励耕织';self.assertFalse(self.request('/api/grade',d)['merged'])
 def test_confirm_score_consistency(self):
  d=copy.deepcopy(engine.EXAMPLE);d['answer']='不知道';r=self.request('/api/grade',d)
  with self.assertRaises(urllib.error.HTTPError) as exc:self.request('/api/confirm',{'id':r['id'],'verdict':'正确','earned':[0,0]})
  self.assertEqual(exc.exception.code,400)
 def test_ocr_is_explicitly_unimplemented(self):
  with self.assertRaises(urllib.error.HTTPError) as exc:self.request('/api/ocr',{'image':'none'})
  self.assertEqual(exc.exception.code,501)
 def test_cross_site_rejected(self):
  req=urllib.request.Request(self.base+'/api/grade',data=b'{}',headers={'Origin':'https://evil.example'})
  with self.assertRaises(urllib.error.HTTPError) as exc:urllib.request.urlopen(req)
  self.assertEqual(exc.exception.code,403)
 def test_static_page(self):
  with urllib.request.urlopen(self.base) as r:self.assertIn('精简模式',r.read().decode())

if __name__=='__main__':unittest.main()
