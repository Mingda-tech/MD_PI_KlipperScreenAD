from pathlib import Path
import re,subprocess,shutil,json,hashlib,sys,importlib.util
base=Path('/mnt/e/MINGDA-3DPrinting-Cloud/tmp/swupdate-arm64-emulated-20261023')
release=base/'cross-candidate-r2/release'
sysroot=base/'rootfs'
baseline={'libc.so.6','libm.so.6','libpthread.so.0','libdl.so.2','librt.so.1','ld-linux-aarch64.so.1'}
checks={}
queue=[release/'swupdate']
while queue:
 path=queue.pop(0)
 data=path.read_bytes()
 assert data[:6]==b'\x7fELF\x02\x01' and int.from_bytes(data[18:20],'little')==183,path
 dynamic=subprocess.check_output(['readelf','-d',str(path)],text=True)
 assert not re.search(r'RPATH|RUNPATH',dynamic),path
 required=re.findall(r'NEEDED.*?\[(.*?)\]',dynamic)
 versions=subprocess.check_output(['readelf','-V',str(path)],text=True)
 glibc=sorted({tuple(map(int,v.split('.'))) for v in re.findall(r'GLIBC_([0-9.]+)',versions)})
 assert not glibc or max(glibc)<=(2,35),(path,glibc)
 checks[path.name]={'machine':'AArch64','sha256':hashlib.sha256(data).hexdigest(),'maxGlibc':'.'.join(map(str,max(glibc))) if glibc else None,'needed':required}
 for name in required:
  if name in baseline or name in checks or any(q.name==name for q in queue):continue
  dest=release/'lib'/name
  if not dest.exists():
   source=sysroot/'usr/lib/aarch64-linux-gnu'/name
   assert source.resolve().is_relative_to(sysroot),source
   shutil.copy2(source,dest,follow_symlinks=True)
  queue.append(dest)
# No symlinks enter the release runtime: policy trust checks reject links.
link=release/'lib/libubootenv.so'
if link.is_symlink():link.unlink()
header=release/'libuboot.h'
if header.exists():header.unlink()
(release/'abi-validation.json').write_text(json.dumps({'schemaVersion':1,'baseline':'Ubuntu22.04 ARM64 glibc2.35','libraries':checks,'systemLibraries':sorted(baseline)},indent=2)+'\n')
print(json.dumps({k:v['maxGlibc'] for k,v in checks.items()},indent=2))
