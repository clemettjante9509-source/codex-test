import json
import math
import os
import sqlite3
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from engine import grade, report, EXAMPLE, ERRORS

ROOT = Path(__file__).parent
DB = Path(os.environ.get('HOMEWORK_DB', str(ROOT / '.data' / 'homework.db')))
LOCK = threading.Lock()

def connect():
 DB.parent.mkdir(parents=True, exist_ok=True)
 db = sqlite3.connect(DB)
 db.row_factory = sqlite3.Row
 db.execute('CREATE TABLE IF NOT EXISTS attempts (id INTEGER PRIMARY KEY, created TEXT DEFAULT CURRENT_TIMESTAMP, result TEXT NOT NULL, confirmed INTEGER NOT NULL DEFAULT 0, group_id INTEGER)')
 return db

def save(r):
 with LOCK, connect() as db:
  previous = db.execute('SELECT * FROM attempts ORDER BY id DESC LIMIT 1').fetchone()
  merged = bool(previous and r['verdict'] != '正确' and r['chapter'] != '教材章节待核对（请填写教材版本与章节）' and (p:=json.loads(previous['result']))['verdict'] != '正确' and p['subject'] == r['subject'] and p['chapter'] == r['chapter'])
  group_id = (previous['group_id'] or previous['id']) if merged else None
  cur = db.execute('INSERT INTO attempts(result,confirmed,group_id) VALUES(?,?,?)', (json.dumps(r, ensure_ascii=False), int(not r['needs_review']), group_id))
  ident = cur.lastrowid
 return ident, merged

def list_errors(filters):
 with connect() as db: rows = db.execute('SELECT * FROM attempts ORDER BY id DESC').fetchall()
 output = []
 for row in rows:
  r = json.loads(row['result'])
  if r['verdict'] == '正确' and row['confirmed']: continue
  if any(filters.get(k, [''])[0] and filters[k][0] != r.get(k) for k in ('subject','chapter','error_type')): continue
  if filters.get('status', [''])[0] == 'confirmed' and not row['confirmed']: continue
  output.append({'id':row['id'], 'created':row['created'], 'confirmed':bool(row['confirmed']), 'group_id':row['group_id'] or row['id'], 'result':r})
 return output

def confirm(data):
 if data.get('verdict') not in ('正确','部分得分','错误'): raise ValueError('请选择复核判定')
 with LOCK, connect() as db:
  row = db.execute('SELECT result FROM attempts WHERE id=?',(int(data['id']),)).fetchone()
  if not row: raise LookupError('记录不存在')
  r = json.loads(row['result']); earned = data.get('earned')
  if not isinstance(earned,list) or len(earned)!=len(r['points']): raise ValueError('须逐条确认采分点分数')
  for p,v in zip(r['points'],earned):
   if not isinstance(v,(int,float)) or isinstance(v,bool) or not math.isfinite(v) or not 0<=v<=p['score']: raise ValueError('复核分值越界')
   p['earned']=v
  score=sum(earned); expected='正确' if score==r['total'] else '错误' if score==0 else '部分得分'
  if expected!=data['verdict']: raise ValueError('总分与判定不一致')
  if expected!='正确' and data.get('error_type') not in ERRORS: raise ValueError('错题必须确认A/B/C/D错误类型')
  r.update(verdict=expected,score=score,needs_review=False,error_type='' if expected=='正确' else data['error_type'],basis='人工复核评分')
  db.execute('UPDATE attempts SET result=?,confirmed=1 WHERE id=?',(json.dumps(r,ensure_ascii=False),data['id']))
 return r

class Handler(BaseHTTPRequestHandler):
 def send(self, code, data, mime='application/json; charset=utf-8'):
  payload = json.dumps(data, ensure_ascii=False).encode() if mime.startswith('application/json') else data.encode() if isinstance(data,str) else data
  self.send_response(code); self.send_header('Content-Type',mime); self.send_header('Content-Length',str(len(payload))); self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(payload)
 def do_GET(self):
  if self.headers.get('Host','').split(':')[0] not in ('127.0.0.1','localhost'): return self.send(403, {'error':'仅允许本机访问'})
  parsed = urlparse(self.path)
  if parsed.path == '/api/config': return self.send(200, {'online':False,'model_available':bool(os.environ.get('GRADING_API_KEY')),'storage_persistent':True})
  if parsed.path == '/api/example': return self.send(200, EXAMPLE)
  if parsed.path == '/api/errors': return self.send(200,list_errors(parse_qs(parsed.query)))
  if parsed.path == '/api/export':
   items = list_errors(parse_qs(parsed.query)); seen = set(); lines = ['# 精简错题清单（含待复核题时标注）']
   for item in items:
    r = item['result']; key = (item['group_id'],r['chapter'])
    if key in seen: continue
    seen.add(key)
    lines.append(f"\n- {r['subject']}｜{r['chapter']}" + ('【待复核】' if not item['confirmed'] else '') + '\n  变式：'+r['variant']['question']+'\n  答案与采分点：'+r['variant']['answer']+'；'+r['variant']['points'])
   self.send_response(200); self.send_header('Content-Type','text/markdown; charset=utf-8'); self.send_header('Content-Disposition','attachment; filename="mistakes.md"'); self.end_headers(); self.wfile.write('\n'.join(lines).encode()); return
  paths = {'/':'index.html','/app.js':'app.js','/style.css':'style.css'}
  if parsed.path in paths:
   mime = {'/':'text/html','/app.js':'text/javascript','/style.css':'text/css'}[parsed.path]
   return self.send(200,(ROOT/'static'/paths[parsed.path]).read_bytes(), mime+'; charset=utf-8')
  self.send(404, {'error':'未找到接口'})
 def do_POST(self):
  # Local single-user tool: reject cross-site browser writes and untrusted hosts.
  host = self.headers.get('Host', '')
  origin = self.headers.get('Origin')
  if origin and urlparse(origin).netloc != host: return self.send(403, {'error':'禁止跨站请求'})
  if host.split(':')[0] not in ('127.0.0.1','localhost'): return self.send(403, {'error':'仅允许本机访问'})
  if self.path == '/api/ocr': return self.send(501, {'error':'OCR接口已预留，尚未配置识别服务。请先输入文字；识别后必须核对原题与答案。','contract':{'request':'未来接收multipart/form-data的image字段，限制10MB并校验文件类型','response':{'question':'string','answer':'string','confidence':'0..1','requires_review':True}}})
  try:
   length = int(self.headers.get('Content-Length','0'))
   if not 0 < length <= 100000: raise ValueError('请求为空或超过100KB')
   data = json.loads(self.rfile.read(length))
   if not isinstance(data,dict): raise ValueError('请求须为JSON对象')
   if self.path == '/api/grade':
    r = grade(data); ident, merged = save(r)
    return self.send(200, {'id':ident,'result':r,'report':report(r,bool(data.get('compact')),merged),'merged':merged})
   if self.path == '/api/report':
    with connect() as db:
     row = db.execute('SELECT result FROM attempts WHERE id=?',(int(data['id']),)).fetchone()
    if not row: return self.send(404, {'error':'记录不存在'})
    r = json.loads(row['result'])
    return self.send(200,{'id':data['id'],'result':r,'report':report(r,bool(data.get('compact')))})
   if self.path == '/api/confirm':
    r = confirm(data)
    return self.send(200,{'result':r,'report':report(r)})
   self.send(404,{'error':'未找到接口'})
  except (ValueError, KeyError, TypeError) as exc: self.send(400, {'error':str(exc)})
  except LookupError as exc: self.send(404, {'error':str(exc)})
  except Exception: self.send(500, {'error':'服务器处理失败，请检查本机日志；未确认成绩'})

if __name__ == '__main__':
 port = int(os.environ.get('PORT','8000'))
 connect().close()
 print(f'作业工具已启动，本机端口 {port}', flush=True)
 ThreadingHTTPServer(('127.0.0.1',port),Handler).serve_forever()
