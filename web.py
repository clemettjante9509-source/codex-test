"""Authenticated single-household web deployment; run behind HTTPS with Gunicorn."""
import hmac
import os
import threading
import time
from collections import deque
from urllib.parse import urlparse
from flask import Flask, jsonify, request, send_from_directory, Response
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.exceptions import HTTPException
from engine import grade, report, EXAMPLE
import server


def create_app(config=None):
 app = Flask(__name__, static_folder=None)
 app.config.update(MAX_CONTENT_LENGTH=100000,
  APP_PASSWORD=os.environ.get('APP_PASSWORD',''),
  ALLOWED_HOSTS=os.environ.get('ALLOWED_HOSTS',os.environ.get('RENDER_EXTERNAL_HOSTNAME','localhost,127.0.0.1')).split(','),
  REQUIRE_HTTPS=os.environ.get('REQUIRE_HTTPS','true').lower()=='true')
 if config: app.config.update(config)
 password = app.config['APP_PASSWORD']
 if not isinstance(password,str) or len(password)<12:
  raise RuntimeError('上线必须设置至少12位APP_PASSWORD；密码只存于托管平台环境变量')
 # Render terminates TLS at its single ingress proxy. Never trust forwarded hosts.
 app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1)
 recent = deque()
 gate = threading.Lock()
 model_gate = threading.BoundedSemaphore(2)

 @app.before_request
 def protect():
  if request.host.split(':')[0] not in app.config['ALLOWED_HOSTS']:
   return jsonify(error='域名不在允许列表'),403
  if request.path == '/health': return None
  if app.config['REQUIRE_HTTPS'] and not request.is_secure:
   return jsonify(error='请使用平台提供的HTTPS网址'),400
  auth = request.authorization
  if not auth or auth.type != 'basic' or not hmac.compare_digest((auth.username or '').encode(),b'homework') or not hmac.compare_digest((auth.password or '').encode(),password.encode()):
   return Response('请输入家庭访问账号和密码。账号：homework',401,
    headers={'WWW-Authenticate':'Basic realm="Homework", charset="UTF-8"'},content_type='text/plain; charset=utf-8')
  if request.method=='POST':
   origin=request.headers.get('Origin')
   if origin and (urlparse(origin).netloc!=request.host or urlparse(origin).scheme!=request.scheme):
    return jsonify(error='禁止跨站请求'),403
   if request.headers.get('Sec-Fetch-Site')=='cross-site': return jsonify(error='禁止跨站请求'),403
   with gate:
    now=time.monotonic()
    while recent and recent[0]<now-60: recent.popleft()
    if len(recent)>=30: return jsonify(error='操作过于频繁，请一分钟后重试'),429
    recent.append(now)

 @app.after_request
 def headers(response):
  response.headers['Cache-Control']='no-store'
  response.headers['X-Content-Type-Options']='nosniff'
  response.headers['X-Frame-Options']='DENY'
  response.headers['Referrer-Policy']='no-referrer'
  response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
  if request.is_secure:response.headers['Strict-Transport-Security']='max-age=31536000'
  return response

 @app.errorhandler(Exception)
 def handle_error(exc):
  if isinstance(exc,HTTPException):return jsonify(error=exc.description),exc.code
  if isinstance(exc,KeyError):return jsonify(error='请求缺少必要字段'),400
  if isinstance(exc,LookupError):return jsonify(error=str(exc)),404
  if isinstance(exc,(ValueError,TypeError)):return jsonify(error=str(exc)),400
  app.logger.error('Request failed (%s)',type(exc).__name__)
  return jsonify(error='服务器处理失败，请重试'),500

 def body():
  data=request.get_json()
  if not isinstance(data,dict):raise ValueError('请求须为JSON对象')
  return data

 @app.get('/health')
 def health():
  with server.connect() as db: db.execute('SELECT 1').fetchone()
  return jsonify(status='ok')

 @app.get('/')
 def index(): return send_from_directory(server.ROOT/'static','index.html')

 @app.get('/app.js')
 def js(): return send_from_directory(server.ROOT/'static','app.js')

 @app.get('/style.css')
 def css(): return send_from_directory(server.ROOT/'static','style.css')

 @app.get('/api/config')
 def config_info():
  return jsonify(online=True, model_available=bool(os.environ.get('GRADING_API_KEY')),
   storage_persistent=os.environ.get('STORAGE_PERSISTENT','false').lower()=='true')

 @app.get('/api/example')
 def example(): return jsonify(EXAMPLE)

 @app.post('/api/grade')
 def grading():
  data=body()
  if data.get('mode')=='model':
   if not model_gate.acquire(blocking=False):return jsonify(error='模型正在处理其他题目，请稍后重试'),429
   try:r=grade(data)
   finally:model_gate.release()
  else:r=grade(data)
  ident,merged=server.save(r)
  return jsonify(id=ident,result=r,report=report(r,bool(data.get('compact')),merged),merged=merged)

 @app.get('/api/errors')
 def errors():return jsonify(server.list_errors(request.args.to_dict(flat=False)))

 @app.post('/api/report')
 def stored_report():
  data=body()
  with server.connect() as db:row=db.execute('SELECT result FROM attempts WHERE id=?',(int(data['id']),)).fetchone()
  if not row:raise LookupError('记录不存在')
  r=server.json.loads(row['result'])
  return jsonify(id=data['id'],result=r,report=report(r,bool(data.get('compact'))))

 @app.post('/api/confirm')
 def confirmation():
  r=server.confirm(body())
  return jsonify(result=r,report=report(r))

 @app.get('/api/export')
 def export():
  items=server.list_errors(request.args.to_dict(flat=False));seen=set();lines=['# 精简错题清单（含待复核题时标注）']
  for item in items:
   r=item['result'];key=(item['group_id'],r['chapter'])
   if key in seen:continue
   seen.add(key)
   lines.append(f"\n- {r['subject']}｜{r['chapter']}"+('【待复核】' if not item['confirmed'] else '')+'\n  变式：'+r['variant']['question']+'\n  答案与采分点：'+r['variant']['answer']+'；'+r['variant']['points'])
  return Response('\n'.join(lines),content_type='text/markdown; charset=utf-8',headers={'Content-Disposition':'attachment; filename="mistakes.md"'})

 @app.post('/api/ocr')
 def ocr():return jsonify(error='OCR接口已预留，尚未配置识别服务。请先输入文字；识别后必须核对原题与答案。'),501

 return app
