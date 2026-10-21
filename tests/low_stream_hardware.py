#!/usr/bin/env python3
"""Run on an RK3588 BSP. Uses synthetic frames and isolated RTSP/RTP ports."""
import argparse, json, os, pathlib, re, subprocess, time

def sessions():
    return pathlib.Path('/proc/mpp_service/sessions-summary').read_text()

def count_low(text):
    return sum(1 for line in text.splitlines() if 'RKVENC' in line and re.search(r'\|\s*640\s*\|\s*360\s*\|',line))

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--build',required=True);args=parser.parse_args()
    root=pathlib.Path(args.build);log=root/'low-hardware-test.log'
    env=dict(os.environ,UAV_LOW_ENABLE='1',UAV_LOW_BIND='127.0.0.1',UAV_LOW_PEER='127.0.0.1',
             UAV_LOW_RTP_PORT='21000',UAV_LOW_CONTROL_PORT='21002')
    workers=[];report={};baseline=count_low(sessions())
    with log.open('w') as output:
        try:
            probe=subprocess.Popen([str(root/'low_probe')],env=env,stdout=output,stderr=output);workers.append(probe)
            relay=subprocess.Popen([str(root/'rtsp_relay_mino17'),'--port','18554','--mount','/test_low','21000','h264',
                                    '--demand','/test_low','127.0.0.1','21002'],stdout=output,stderr=output);workers.append(relay)
            time.sleep(1)
            assert probe.poll() is None and relay.poll() is None
            assert count_low(sessions())==baseline,'low encoder must be OFF before a subscriber'
            report['idle_before']=True
            argv=['ffmpeg','-nostdin','-hide_banner','-loglevel','info','-rtsp_transport','tcp','-stimeout','6000000',
                  '-i','rtsp://127.0.0.1:18554/test_low','-an','-frames:v','30','-f','null','-']
            begin=time.monotonic()
            first=subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE);workers.append(first)
            time.sleep(1.2)
            assert count_low(sessions())==baseline+1,'subscriber must start one low encoder'
            second=subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE);workers.append(second)
            one=first.communicate(timeout=15);assert first.returncode==0,one[1].decode()[-1500:]
            assert count_low(sessions())==baseline+1,'another subscriber must keep the shared encoder running'
            two=second.communicate(timeout=15);assert second.returncode==0,two[1].decode()[-1500:]
            assert b'640x360' in one[1] and b'640x360' in two[1]
            report.update(two_clients_decode=True,first_30_frames_seconds=round(time.monotonic()-begin,2))
            time.sleep(6.5)
            assert count_low(sessions())==baseline,'last subscriber must release the encoder after the lease'
            report['idle_after']=True
            again=subprocess.run(argv,capture_output=True,timeout=15)
            assert again.returncode==0,again.stderr.decode()[-1500:]
            report['reopen_decode']=True
        finally:
            for child in reversed(workers):
                if child.poll() is None:
                    child.terminate()
                    try:child.wait(timeout=5)
                    except subprocess.TimeoutExpired:child.kill();child.wait()
    print(json.dumps(report,indent=2));return 0

if __name__=='__main__':raise SystemExit(main())
