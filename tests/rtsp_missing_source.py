import argparse, json, pathlib, socket, subprocess, tempfile, time
parser=argparse.ArgumentParser(description='Compare old/fixed RTSP relay with one missing RTP source')
parser.add_argument('--old-bin', required=True)
parser.add_argument('--fixed-bin', required=True)
args=parser.parse_args()
BASE=pathlib.Path(tempfile.mkdtemp(prefix='uav-rtsp-regression-'))
print('test logs:',BASE)
PORT=18554
RTP=15702
for kind,port in [(socket.SOCK_STREAM,PORT),(socket.SOCK_DGRAM,RTP),(socket.SOCK_DGRAM,15602)]:
    with socket.socket(socket.AF_INET,kind) as sock:sock.bind(('127.0.0.1',port))
def reader(path):
    return ['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-rtsp_transport','tcp','-stimeout','3000000','-analyzeduration','200000','-probesize','10000','-i',f'rtsp://127.0.0.1:{PORT}/{path}','-frames:v','1','-f','null','-']
def decode():
    try:
        p=subprocess.run(reader('live'),stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True,timeout=5)
        return p.returncode==0,p.stderr[-400:]
    except subprocess.TimeoutExpired:return False,'timeout'
def case(binary,label):
    procs=[]
    with (BASE/(label+'.log')).open('w') as log:
        try:
            procs.append(subprocess.Popen([binary,'--port',str(PORT),'--mount','/live',str(RTP),'h264','--mount','/missing','15602','h264'],stdout=log,stderr=log))
            procs.append(subprocess.Popen(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-re','-f','lavfi','-i','testsrc2=size=160x120:rate=25','-c:v','libx264','-threads','1','-preset','ultrafast','-tune','zerolatency','-g','5','-x264-params','repeat-headers=1','-bsf:v','dump_extra','-an','-f','rtp',f'rtp://127.0.0.1:{RTP}'],stdout=log,stderr=log))
            time.sleep(1)
            before,detail=decode()
            if not before:raise RuntimeError('baseline failed '+label+': '+detail)
            procs.append(subprocess.Popen(reader('missing'),stdout=log,stderr=log))
            time.sleep(1)
            after,detail=decode()
            result={'version':label,'before_missing_request':before,'after_missing_request':after,'detail':detail}
            print(json.dumps(result,ensure_ascii=False),flush=True)
            return result
        finally:
            for p in reversed(procs):
                if p.poll() is None:p.terminate()
            for p in procs:
                try:p.wait(timeout=3)
                except subprocess.TimeoutExpired:p.kill();p.wait()
results=[case(str(pathlib.Path(args.old_bin).resolve()),'old'),case(str(pathlib.Path(args.fixed_bin).resolve()),'fixed')]
(BASE/'comparison.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
assert not results[0]['after_missing_request'], '未复现旧版阻塞'
assert results[1]['after_missing_request'], '修复未通过'
print('PASS: missing RTP source no longer blocks healthy mount')
