"""End-to-end IRCTC flow on a fresh Comet lease. Never pays.
Usage: python3 book.py FROM TO DD/MM/YYYY QUOTA CLASS_LABEL TRAIN_NO PASSENGER_PREFIX [--at HH:MM:SS] [--confirm-pay]
 e.g.  python3 book.py FROM TO DD/MM/YYYY GENERAL "AC Chair car (CC)" TRAIN_NO PREFIX
QUOTA: GENERAL|TATKAL. Default stops on the Pay & Book screen and hands over to Guru.
--confirm-pay clicks IRCTC iPay New then Pay & Book only after Guru confirms that booking in chat.
That click opens the iPay gateway and does not charge. Never UPI, Google Pay, card, or Pay Now.
Leaves the lease open; prints WORK for closeout."""
import json, subprocess, sys, os, time, pathlib
HERE=os.path.dirname(os.path.abspath(__file__)); RT=os.path.expanduser('~/.agents/plugins/comet-control')
CTL=['python3','skills/comet-control/scripts/durable_lease_controller.py']
def parse_book_args(argv):
    """Return (positionals, at, confirm_pay). --confirm-pay is off unless present."""
    args=list(argv); at=None; confirm_pay=False
    if '--at' in args:
        i=args.index('--at')
        if i+1>=len(args) or str(args[i+1]).startswith('--'): sys.exit('FAIL: --at needs HH:MM:SS')
        at=args[i+1]; del args[i:i+2]
    while '--confirm-pay' in args:
        confirm_pay=True; args.remove('--confirm-pay')
    return args, at, confirm_pay
def pay_book_actions(confirm_pay):
    """IRCTC iPay New then Pay & Book, or None to stop before the click.
    The click opens the gateway and does not charge. No UPI, Google Pay, card, or Pay Now."""
    if not confirm_pay: return None
    return [
        {"type":"click_selector","selector":"div.border-all.col-xs-12.link.no-pad"},
        {"type":"wait","ms":500},
        {"type":"click_text","text":"Pay & Book"},
    ]
if __name__ == '__main__':
    args, AT, CONFIRM_PAY = parse_book_args(sys.argv[1:])
    if len(args)<7: sys.exit('FAIL: PASSENGER_PREFIX required')
    fr,to,date,quota,cls,train=args[:6]; pfx=args[6]
    T0=time.time()
    def log(m): print(f'[{time.time()-T0:6.1f}s] {m}', flush=True)
    WORK=None
    def die(m):
        log('FAIL: '+m)
        log('window left open for the passenger: '+str(WORK))
        sys.exit(1)
    POP="Array.from(document.querySelectorAll('button,a')).filter(e=>e.offsetParent!==null&&/^(Close|OK|Ok|Okay|No Thanks|Later)$/.test(e.innerText.trim())).map(e=>e.innerText.trim())"
    def _raw(actions, timeout=90):
        f='/tmp/irctc/step.json'; os.makedirs('/tmp/irctc',exist_ok=True); json.dump({"actions":actions},open(f,'w'))
        r=subprocess.run(CTL+['send','--timeout',str(timeout),'--workdir',WORK,'--payload-file',f],cwd=RT,capture_output=True,text=True)
        try: return json.loads(r.stdout.strip().splitlines()[-1]).get('response',{})
        except Exception: return {'success':False,'error_code':'NO_REPLY','error':(r.stderr or r.stdout)[-300:]}
    def clear_popups():
        """Close any visible popup (IRCTC last-transaction, alerts, offers)."""
        for _ in range(3):
            r=_raw([{"type":"evaluate","expression":POP}])
            btns=((r.get('results') or [{}])[0].get('result') or []) if r.get('success') else []
            if not btns:
                _raw([{"type":"cursor_key","key":"Escape","modifiers":[]}]); return
            r2=_raw([{"type":"click_text","text":btns[0]},{"type":"wait","ms":500}])
            if not r2.get('success'): _raw([{"type":"cursor_key","key":"Escape","modifiers":[]},{"type":"wait","ms":400}])
            log('closed popup button: '+btns[0])
    RETRY={'ACTIONABILITY_UNSTABLE','ACTIONABILITY_OBSCURED','ACTIONABILITY_TARGET_COUNT','ACTIONABILITY_DISABLED','TIMEOUT','NO_REPLY',None}
    def send(actions, timeout=90, tries=4):
        resp=_raw(actions,timeout)
        if resp.get('success'):
            return [x.get('result') for x in resp.get('results',[]) if x.get('type')=='evaluate'], resp.get('final_url')
        code=resp.get('error_code'); err=resp.get('error') or ''
        act=(resp.get('failure_record') or {}).get('action') or {}; i=act.get('index',0)
        if tries>1 and (code in RETRY) and 'side-effect' not in err:
            log(f'retry ({tries-1} left) after {code} at step {i}')
            sr=_raw([{"type":"screenshot"}]); log('failure screenshot: '+str(((sr.get('results') or [{}])[0]).get('screenshot_path')))
            clear_popups(); time.sleep(1.5 if code=='ACTIONABILITY_DISABLED' else 0.6)
            return send(actions[i:],timeout,tries-1)
        shot=_raw([{"type":"screenshot"},{"type":"evaluate","expression":"location.pathname+' | '+document.body.innerText.slice(0,300)"}])
        for x in shot.get('results',[]):
            if x.get('type')=='screenshot': log('screenshot: '+str(x.get('screenshot_path')))
            if x.get('type')=='evaluate': log('page: '+str(x.get('result')).replace('\n',' '))
        die(f"{code} {err[:150]} at {json.dumps(act)}")
    def K(k,m=None): return {"type":"cursor_key","key":k,"modifiers":m or []}
    def C(s): return {"type":"click_selector","selector":s}
    def W(ms): return {"type":"wait","ms":ms}
    def E(x): return {"type":"evaluate","expression":x}
    def TY(t): return {"type":"cursor_type","text":t}
    def WS(sel,ms=15000): return {"type":"wait_for_selector","selector":sel,"timeout":ms}
    def poll(expr,ok,secs=20):
        end=time.time()+secs
        while time.time()<end:
            v=send([E(expr)])[0][0]
            if ok(v): return v
            time.sleep(0.3)
        die('timeout waiting: '+expr[:60])
    pr=subprocess.run(['./scripts/ensure-broker.sh','probe','--json'],cwd=RT,capture_output=True,text=True)
    if '"extension_connected": true' not in pr.stdout: die('broker probe not healthy')


    LF=os.path.expanduser('~/irctc/.lease'); WORK=None
    if os.path.exists(LF):
        w=open(LF).read().strip()
        st=subprocess.run(CTL+['status','--workdir',w],cwd=RT,capture_output=True,text=True).stdout
        if '"alive": true' in st:
            WORK=w
            url=send([E("location.pathname")])[0][0] or ''
            if 'train-search' in url: log(f'reusing window WORK={WORK}')
            else:
                subprocess.run(CTL+['closeout','--workdir',WORK],cwd=RT,capture_output=True); log('old window not on search page; closed it'); WORK=None
    if not WORK:
        sid=f'irctc-{int(time.time())}'; WORK=f'/tmp/comet-control-{sid}'
        r=subprocess.run(CTL+['start','--session-id',sid,'--label','Travel IRCTC','--url','https://www.irctc.co.in/nget/train-search','--workdir',WORK,'--ttl-seconds','3600'],cwd=RT,capture_output=True,text=True)
        if '"ok": true' not in r.stdout: die('lease start failed')
        open(LF,'w').write(WORK); log(f'lease ready WORK={WORK}')
    if send([E("Array.from(document.querySelectorAll('.ui-dialog')).filter(e=>e.offsetParent!==null).length")])[0][0]:
        send([W(500),{"type":"click_text","text":"English"},W(400)])
    hdr=send([E("((document.body.innerText.match(/Welcome [A-Z][^|\\n]*\\(/)||[''])[0]) || ((/log ?out/i.test(document.body.textContent)||/Welcome [A-Z]/.test(document.body.textContent))?'LOGOUT present':'')")])[0][0] or ''
    if hdr: log('already logged in: '+hdr.strip()); LOGGED=True
    else:
        LOGGED=False
        vis=send([E("Array.from(document.querySelectorAll('a,button')).filter(e=>e.offsetParent!==null&&e.innerText.trim()=='LOGIN / REGISTER').length+'|'+window.innerWidth")])[0][0]
        n,wid=vis.split('|')
        if n=='0':  # narrow window: login sits behind the hamburger (top right)
            send([{"type":"cursor_scroll","deltaX":0,"deltaY":-4000},W(400),{"type":"cursor_move","x":int(wid)-33,"y":95},{"type":"cursor_click"},W(1000)])
        n2=send([E("Array.from(document.querySelectorAll('a,button')).filter(e=>e.offsetParent!==null&&e.innerText.trim()=='LOGIN / REGISTER').length")])[0][0]
        if not n2:
            send([W(800)]); n2=send([E("Array.from(document.querySelectorAll('a,button')).filter(e=>e.offsetParent!==null&&e.innerText.trim()=='LOGIN / REGISTER').length")])[0][0]
        for _ in range(3):
            if n2: break
            log('LOGIN not visible yet; re-opening menu')
            clear_popups(); send([{"type":"cursor_scroll","deltaX":0,"deltaY":-4000},W(400),{"type":"cursor_move","x":int(wid)-33,"y":95},{"type":"cursor_click"},W(1200)])
            n2=send([E("Array.from(document.querySelectorAll('a,button')).filter(e=>e.offsetParent!==null&&e.innerText.trim()=='LOGIN / REGISTER').length")])[0][0]
        if not n2: die('LOGIN / REGISTER not found and not logged in')
        send([{"type":"click_text","text":"LOGIN / REGISTER"},WS("input[formcontrolname='userid']")])
    if not LOGGED:
        env=dict(os.environ,WORK=WORK)
        if subprocess.run(['python3',os.path.join(HERE,'login.py')],env=env).returncode: die('login fill')
        send([W(500),C('app-login button.search_btn')])
        v=poll("((document.querySelector(\"input[formcontrolname='userid']\")||{}).offsetParent?'DIALOG ':'')+((document.body.innerText.match(/Welcome [^|\\n]*/)||['?'])[0])",lambda v:not (v or 'DIALOG').startswith('DIALOG'))
        log('logged in: '+v.strip())
    send([
     C("input[aria-label='Enter From station. Input is Mandatory.']"),K('a',['Meta']),TY(fr),WS('.ui-autocomplete-panel li'),W(300),K('Enter'),W(200),
     {"type":"cursor_key","key":"Escape"},W(300),C("input[aria-label='Enter To station. Input is Mandatory.']"),K('a',['Meta']),TY(to),WS('.ui-autocomplete-panel li'),W(300),K('Enter'),W(200),
     C('p-calendar input'),K('a',['Meta']),TY(date),K('Escape'),W(200),
     *([] if quota=='GENERAL' else [C('#journeyQuota'),WS("#journeyQuota li[aria-label]",5000),W(350),C(f"#journeyQuota li[aria-label='{quota}']"),W(200)]),
     C('#journeyClass'),WS("#journeyClass li[aria-label]",5000),W(350),C(f"#journeyClass li[aria-label='{cls}']"),W(200),
     E("document.querySelector('#journeyClass').innerText+' / '+document.querySelector('#journeyQuota').innerText")])
    log('search form filled')
    if AT:
        import datetime as dt
        h,m,sec=map(int,AT.split(':')); now=dt.datetime.now(); tgt=now.replace(hour=h,minute=m,second=sec,microsecond=0)
        if tgt<now: die(f'--at {AT} already passed')
        log(f'waiting for {AT}'); time.sleep((tgt-dt.datetime.now()).total_seconds())
        log('GO')
    ev,url=send([C("button.search_btn.train_Search"),{"type":"wait_for_url_change","from_url":"https://www.irctc.co.in/nget/train-search","timeout":20000},WS('app-train-avl-enq',20000),W(500),
     E("Array.from(document.querySelectorAll('app-train-avl-enq')).map(e=>{var g=e.parentElement.parentElement;return [Array.prototype.indexOf.call(g.parentElement.children,g)+1, e.innerText.split('\\n')[0], Array.from(e.querySelectorAll('td strong')).map(s=>s.innerText)]})")])
    cards=ev[0]; log(f'{len(cards)} trains listed')
    hit=[c for c in cards if f'({train})' in c[1]] or die(f'train {train} not in list')
    n,name,classes=hit[0]
    if cls not in classes: die(f'{cls} not on {name}: {classes}')
    c=classes.index(cls)+1; card=f'div.ng-star-inserted:nth-child({n}) > div > app-train-avl-enq'
    send([C(f'{card} td:nth-child({c}) div.pre-avl')])
    avl=poll(f"document.querySelector('{card}').innerText.replace(/\\n+/g,' | ').slice(0,600)",lambda v:any(k in (v or '') for k in ('AVAILABLE','WL','REGRET','RAC','ARP','NOT AVAILABLE')))
    log('availability: '+avl[avl.find(cls):][:160])
    if 'Tatkal ARP' in avl: die('Tatkal not open yet for this date')
    send([C(f'{card} td.link:nth-child(2) div.pre-avl'),W(300),C(f'{card} button.btnDefault.train_Search'),{"type":"wait_for_url_change","from_url":"https://www.irctc.co.in/nget/booking/train-list","timeout":20000},WS("input[placeholder='Full Name as per Govt. ID']")])
    log('passenger page')
    ev,_=send([C("input[placeholder='Full Name as per Govt. ID']"),K('a',['Meta']),TY(pfx),WS('.ui-autocomplete-panel li.ui-autocomplete-list-item',5000),C('.ui-autocomplete-panel li.ui-autocomplete-list-item:first-child'),W(300),
     C("p-radiobutton[formcontrolname=paymentType][id='2'] .ui-radiobutton-box"),W(200),
     E("Array.from(document.querySelectorAll('p-radiobutton')).map(r=>(r.querySelector('.ui-state-active')?'x':'-')+r.id).join(' ')")])
    log('passenger + UPI set: '+ev[0])
    ev,_=send([{"type":"click_text","text":"Continue"},{"type":"wait_for_url_change","from_url":"https://www.irctc.co.in/nget/booking/psgninput","timeout":45000},W(300),
     E("location.pathname+' '+(document.body.innerText.match(/Total Fare\\n[^\\n]*/)||[''])[0].replace('\\n',' ')")])
    log('review: '+ev[0])
    # Review page: Continue stays disabled until the captcha is typed. The passenger types it; we wait (no retries/clicks).
    # Tatkal review: Aadhaar consent + OTP (verified 2026-10-11). Tick, Generate OTP, then the passenger gives/types the 6-digit OTP.
    if send([E("document.querySelectorAll(\"input[formcontrolname=aadhaarConsent]\").length")])[0][0]:
        watcher=None
        if os.environ.get('IRCTC_OTP_FILE'):
            pathlib.Path(os.environ['IRCTC_OTP_FILE']).unlink(missing_ok=True)  # clear a stale OTP; absent is the normal case
            watcher=subprocess.Popen(['python3',os.path.join(HERE,'otp_watch.py'),os.environ['IRCTC_OTP_FILE']]); time.sleep(1.5)  # baseline before new SMS
        send([C("input[formcontrolname=aadhaarConsent]"),W(400),{"type":"click_text","text":"Generate OTP"},W(800)])
        log('AADHAAR OTP requested: '+str(send([E("Array.from(document.querySelectorAll('.ui-toast,.ui-messages,.ui-growl')).map(e=>e.innerText.trim()).join(' / ')")])[0][0]))
        if os.environ.get('IRCTC_OTP_FILE'):
            f=os.environ['IRCTC_OTP_FILE']; log('waiting for OTP in '+f); end=time.time()+240
            while time.time()<end and not (os.path.exists(f) and open(f).read().strip()): time.sleep(0.2)
            otp=open(f).read().strip() if os.path.exists(f) else ''
            if len(otp)==6 and otp.isdigit():
                acts=[]
                for i,d in enumerate(otp): acts+=[C(f"#otp-{i}"),K('a',['Meta']),TY(d),W(150)]
                send(acts+[W(400),{"type":"click_text","text":"Continue"}])
                os.remove(f); log('OTP entered, Continue clicked')
    log('REVIEW PAGE: passenger, enter the OTP (or captcha) and click Continue if not done (waiting up to 4 min)')
    v=poll("location.pathname",lambda v:'reviewBooking' not in (v or ''),secs=240)
    log('left review page: '+str(v))
    if 'bkgPaymentOptions' not in (v or ''):
        log('not on payment options yet (Aadhaar/OTP step?) - waiting up to 4 more min for the passenger')
        v=poll("location.pathname",lambda v:'bkgPaymentOptions' in (v or '') or 'payment' in (v or '').lower(),secs=240)
    # Pay & Book only after Guru confirms this booking (--confirm-pay). Default hands over on this screen.
    # The click selects IRCTC iPay New and opens the gateway. It does not charge. Never UPI, Google Pay, card, or Pay Now.
    actions=pay_book_actions(CONFIRM_PAY)
    if actions is None:
        log('PAY & BOOK screen: stopped before the click. Hand over to Guru. --confirm-pay only after Guru confirms this booking in chat. WORK='+WORK)
    else:
        send(actions)
        v=poll("location.host",lambda v:'irctcpayments' in (v or ''),secs=30)
        log('GATEWAY PAGE reached ('+str(v)+'): STOP. No UPI, Google Pay, or card. Guru pays himself. WORK='+WORK)
