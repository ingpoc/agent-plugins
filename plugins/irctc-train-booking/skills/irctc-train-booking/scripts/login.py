"""Type IRCTC username/password from Mini Keychain (service irctc) into the open login dialog. Never prints secrets.
Usage: WORK=/tmp/comet-control-<session> python3 login.py   (login dialog must already be open)"""
import json, subprocess, os, sys, glob
work=os.environ.get('WORK') or sys.exit('FAIL: set WORK')
def kc(a): return subprocess.run(['security','find-generic-password','-s','irctc','-a',a,'-w'],capture_output=True,text=True,check=True).stdout.rstrip('\n')
u,p=kc('username'),kc('password')
if not (u and p): sys.exit('FAIL: Keychain irctc username/password empty')
acts={"actions":[
 {"type":"click_selector","selector":"input[formcontrolname='userid']"},{"type":"cursor_key","key":"a","modifiers":["Meta"]},{"type":"cursor_type","text":u},
 {"type":"click_selector","selector":"input[formcontrolname='password']"},{"type":"cursor_key","key":"a","modifiers":["Meta"]},{"type":"cursor_type","text":p}]}
os.makedirs('/tmp/irctc',exist_ok=True); f='/tmp/irctc/.lp.json'
fd=os.open(f,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600); os.write(fd,json.dumps(acts).encode()); os.close(fd)
try:
  r=subprocess.run(['python3','skills/comet-control/scripts/durable_lease_controller.py','send','--timeout','60','--workdir',work,'--payload-file',f],cwd=os.path.expanduser('~/.agents/plugins/comet-control'),capture_output=True,text=True)
finally:
  os.remove(f)
  for fp in glob.glob(work+'/controller.log')+glob.glob(work+'/response.json'):
    s=open(fp,errors='ignore').read()
    if u in s or p in s: open(fp,'w').write(s.replace(p,'[REDACTED]').replace(u,'[REDACTED_USER]'))
try: ok=json.loads(r.stdout.strip().splitlines()[-1]).get('response',{}).get('success')
except Exception: ok=False
print('login fill ok' if ok else 'FAIL: login fill'); sys.exit(0 if ok else 1)
