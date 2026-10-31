from pathlib import Path
import re,urllib.request,hashlib,shutil,json,subprocess
base=Path('/mnt/e/MINGDA-3DPrinting-Cloud/tmp/swupdate-arm64-emulated-20261023')
release=base/'cross-candidate-r2/release'
sources=release/'dependency-sources'
licenses=release/'dependency-licenses'
sources.mkdir(exist_ok=True);licenses.mkdir(exist_ok=True)
rows=[('openssl','main/o/openssl','3.0.2-0ubuntu1.30','libssl3'),('json-c','main/j/json-c','0.15-3~ubuntu1.22.04.2','libjson-c5'),('libconfig','universe/libc/libconfig','1.5-0.4build1','libconfig9'),('libyaml','main/liby/libyaml','0.2.2-1build2','libyaml-0-2')]
records=[]
def fetch(url,dest,expected=None):
 if not dest.exists():
  with urllib.request.urlopen(url,timeout=60) as response,dest.open('wb') as out:shutil.copyfileobj(response,out)
 digest=hashlib.sha256(dest.read_bytes()).hexdigest()
 assert expected is None or expected==digest,(url,expected,digest)
 return {'file':str(dest.relative_to(release)),'url':url,'sha256':digest}
for name,pool,version,doc in rows:
 folder=sources/name;folder.mkdir(exist_ok=True)
 url='https://ports.ubuntu.com/ubuntu-ports/pool/'+pool+'/'
 dsc=folder/(name+'_'+version+'.dsc')
 records.append(fetch(url+dsc.name,dsc))
 match=re.search(r'Checksums-Sha256:\n((?: [^\n]+\n)+)',dsc.read_text())
 assert match,name
 for line in match.group(1).splitlines():
  digest,size,filename=line.split()
  record=fetch(url+filename,folder/filename,digest)
  assert (folder/filename).stat().st_size==int(size)
  records.append(record)
 copyright=base/'rootfs/usr/share/doc'/doc/'copyright'
 assert copyright.resolve().is_relative_to(base/'rootfs')
 shutil.copy2(copyright,licenses/(name+'.copyright'))
folder=sources/'libubootenv';folder.mkdir(exist_ok=True)
for name in ('libubootenv_0.3.5.orig.tar.xz','libubootenv.dsc'):
 shutil.copy2(base/'downloads'/name,folder/name)
for name in ('LICENSES','Licenses'):
 path=base/'libubootenv-source'/name
 if path.is_dir():shutil.copytree(path,licenses/'libubootenv',dirs_exist_ok=True)
for name in ('COPYING','COPYING.LGPL-2.1'):
 path=base/'libubootenv-source'/name
 if path.exists():shutil.copy2(path,licenses/('libubootenv.'+name))
source=base/'cross-candidate-r2/source'
shutil.copy2(source/'COPYING',release/'COPYING.swupdate')
shutil.copytree(source/'LICENSES',release/'LICENSES',dirs_exist_ok=True)
(release/'dependency-sources.json').write_text(json.dumps({'schemaVersion':1,'files':records,'libubootenvSourceSha256':'95b747e37661bd2e71a1ef34ecddf62af7a42a71459381e2599e8c7aac52770c'},indent=2)+'\n')
print('Dependency source archives and licenses saved:',len(records))
