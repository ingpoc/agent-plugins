"""Watch the iPhone Mirroring Messages screen for a NEW Aadhaar OTP and write it to argv[1] the moment it appears.
Baseline = OTP visible at start (old one); exits after writing the first different OTP, or after 300 s.
Needs iPhone Mirroring open on Messages > Transactions (window at 2052,326)."""
import subprocess, re, sys, time, os
out=sys.argv[1]; HERE=os.path.dirname(os.path.abspath(__file__)); IMG='/tmp/irctc/otp_top.png'
os.makedirs('/tmp/irctc',exist_ok=True)
R=re.compile(r'(\d{6})\s*is\s*OTP\s*for\s*Aadhaar',re.I)
def bounds():
    b=subprocess.run(['osascript','-e','tell application "System Events" to tell process "iPhone Mirroring" to get {position, size} of window 1'],capture_output=True,text=True).stdout
    try: x,y,w,h=[int(v) for v in b.replace(' ','').split(',')]; return f'-R{x},{y+54},{w},180'
    except Exception: return '-R2052,380,322,180'
REG=None
def read():
    subprocess.run(['screencapture','-x',REG,IMG])
    t=subprocess.run([os.path.join(HERE,'ocr'),IMG,'accurate'],capture_output=True,text=True).stdout
    m=R.search(t); return m.group(1) if m else None
subprocess.run(['osascript','-e','tell application "iPhone Mirroring" to activate'])
time.sleep(0.4); REG=bounds(); base=read(); print('baseline',base,flush=True); end=time.time()+300
while time.time()<end:
    o=read()
    if o and o!=base:
        open(out,'w').write(o); print('new OTP captured',flush=True); break
    time.sleep(0.3)
subprocess.run(['osascript','-e','tell application "Comet" to activate'])
