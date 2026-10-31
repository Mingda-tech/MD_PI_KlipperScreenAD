from pathlib import Path
import subprocess,json,os,hashlib,sys,importlib.util,re,struct,shutil,tempfile
base=Path('/mnt/e/MINGDA-3DPrinting-Cloud/tmp/swupdate-arm64-emulated-20261023')
release=base/'cross-candidate-r2/release'
evidence=release/'validation'
evidence.mkdir(exist_ok=True)
key=Path('/mnt/e/MINGDA-3DPrinting-Cloud/.tmp/offline-update-signing-20261023/release-public.pem')
package=key.parent/'release-smoke.mdu'
qemu=base/'hosttools/usr/bin/qemu-aarch64-static'
sysroot=base/'rootfs'
work=Path(tempfile.mkdtemp(prefix='mingda-a64-check-'))
work.mkdir(exist_ok=True)
engine=[str(qemu),'-L',str(sysroot),'-E','LD_LIBRARY_PATH='+str(release/'lib'),str(release/'swupdate')]
results={}
def run(name,args,expect_success,sku='M1D_STANDARD'):
 job=work/name
 job.mkdir(exist_ok=True)
 (job/'tmp').mkdir(exist_ok=True)
 (job/'run').mkdir(exist_ok=True)
 cfg=job/'swupdate.cfg'
 cfg.write_text('globals = { bootloader = "none"; reboot = "disabled"; loglevel = 3; };\n')
 env={'PATH':'/usr/bin:/bin','HOME':'/tmp','LANG':'C.UTF-8','TMPDIR':str(job/'tmp'),'RUNTIME_DIRECTORY':str(job/'run'),'MINGDA_JOB_DIR':str(job)}
 cmd=engine+['-f',str(cfg),'-i',str(args),'-k',str(key),'-H','MINGDA:'+sku+'-X527-aarch64-ubuntu-22.04','-e','stable,main','-m','-M','-c']
 result=subprocess.run(cmd,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=30)
 (evidence/(name+'.log')).write_bytes(result.stdout)
 assert (result.returncode==0)==expect_success,(name,result.returncode,result.stdout.decode(errors='replace'))
 assert not (job/'installed-demo.txt').exists()
 results[name]={'passed':True,'returncode':result.returncode,'checkOnly':True}
for option in ('--version','--help'):
 result=subprocess.run(engine+[option],capture_output=True,timeout=10,check=True)
 (evidence/(option[2:]+'.txt')).write_bytes(result.stdout+result.stderr)
 if option=='--version':assert re.fullmatch(rb'SWUpdate v2026\.05(?:\.0)?\s*',result.stdout)
 else:
  for flag in (b'--check',b'--key',b'--hwrevision',b'--preupdate',b'--no-transaction-marker'):
   assert flag in result.stdout+result.stderr,flag
run('signed-standard',package,True)
run('signed-ultra',package,True,'M1D_ULTRA')
run('wrong-hardware',package,False,'UNSUPPORTED')
data=package.read_bytes()
def members(raw):
 offset=0
 while offset<len(raw):
  start=offset
  assert raw[start:start+6]==b'070701'
  fields=[int(raw[start+6+i*8:start+14+i*8],16) for i in range(13)]
  size=fields[6];length=fields[11]
  name=raw[start+110:start+110+length-1].decode()
  pos=(start+110+length+3)&~3
  end=(pos+size+3)&~3
  yield name,pos,size,start,end
  if name=='TRAILER!!!':break
  offset=end
rows=list(members(data))
for name,member in (('tampered-payload','demo.txt'),('bad-signature','sw-description.sig')):
 mutable=bytearray(data)
 row=next(row for row in rows if row[0]==member)
 mutable[row[1]]^=1
 path=work/(name+'.mdu');path.write_bytes(mutable)
 run(name,path,False)
sig=next(row for row in rows if row[0]=='sw-description.sig')
# Member boundaries preserve the CPIO 4-byte alignment.
unsigned=work/'unsigned.mdu';unsigned.write_bytes(data[:sig[3]]+data[sig[4]:])
run('missing-signature',unsigned,False)
(evidence/'checks.json').write_text(json.dumps({'architecture':'aarch64','targetOs':'ubuntu22.04','execution':'QEMU8.2.2 user mode','packageSha256':hashlib.sha256(data).hexdigest(),'publicKeySha256':hashlib.sha256(key.read_bytes()).hexdigest(),'tests':results,'physicalHardwareTested':False},indent=2)+'\n')
print(json.dumps(results,indent=2))
