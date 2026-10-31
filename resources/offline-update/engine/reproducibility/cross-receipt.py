from pathlib import Path
import subprocess,json,hashlib,importlib.util,struct,shutil
base=Path('/mnt/e/MINGDA-3DPrinting-Cloud/tmp/swupdate-arm64-emulated-20261023')
release=base/'cross-candidate-r2/release'
spec=importlib.util.spec_from_file_location('engine_receipt',base/'build-tools/engine-receipt.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
features=module.check_config(release/'swupdate.config')
commit=subprocess.check_output(['git','-C','/mnt/e/MINGDA-3DPrinting-Cloud/tmp/swupdate-2026.05-source','rev-parse','HEAD'],text=True).strip()
assert commit==module.COMMIT
binary=(release/'swupdate').read_bytes()
assert binary[:6]==b'\x7fELF\x02\x01' and struct.unpack('<H',binary[18:20])[0]==183
checks=json.loads((release/'validation/checks.json').read_text())
assert len(checks['tests'])==6 and all(row['passed'] for row in checks['tests'].values())
abi=json.loads((release/'abi-validation.json').read_text())
for name,row in abi['libraries'].items():
 path=release/'swupdate' if name=='swupdate' else release/'lib'/name
 assert module.sha256(path)==row['sha256']
 assert tuple(map(int,row['maxGlibc'].split('.')))<=(2,35)
compiler=subprocess.check_output(['aarch64-linux-gnu-gcc','-dumpfullversion'],text=True).strip()
qemu=subprocess.check_output([str(base/'hosttools/usr/bin/qemu-aarch64-static'),'--version'],text=True).splitlines()[0]
receipt={'schemaVersion':1,'version':module.VERSION,'sourceCommit':commit,'configSha256':module.sha256(release/'swupdate.config'),'binarySha256':module.sha256(release/'swupdate'),'arch':'aarch64','features':features,'socketPaths':{'control':'','progress':''},'developmentBuild':False,'buildType':'cross-validated','buildEnvironment':{'host':'Ubuntu24.04 x86_64','compiler':'aarch64-linux-gnu-gcc '+compiler,'target':'Ubuntu22.04 ARM64','sysrootGlibc':'2.35','headerIsolation':'-nostdinc; GCC builtin headers and Jammy target headers only','executionValidator':qemu,'nativeArm64CompilerAttempt':'QEMU/PRoot collect2 crashed; no outputs from that attempt used'},'validation':{'abiManifestSha256':module.sha256(release/'abi-validation.json'),'packageChecksSha256':module.sha256(release/'validation/checks.json'),'maxAllowedGlibc':'2.35','physicalHardwareTested':False}}
(release/'build-info.json').write_text(json.dumps(receipt,indent=2,sort_keys=True)+'\n')
(release/'version.txt').write_text((release/'validation/version.txt').read_text())
(release/'runtime-libraries.txt').write_text('Target baseline: Ubuntu 22.04 ARM64, system libc/ld-linux (not bundled).\n'+'\n'.join(name+' => lib/'+name+' SHA256='+row['sha256'] for name,row in abi['libraries'].items() if name!='swupdate')+'\n')
inputs=[]
for path in sorted((base/'apt/cache/archives').glob('*.deb')):
 inputs.append({'file':path.name,'sha256':module.sha256(path)})
(release/'build-inputs.json').write_text(json.dumps({'schemaVersion':1,'source':'Ubuntu official Jammy/main+universe, jammy-updates and jammy-security; signed apt InRelease verified by Ubuntu archive keyring','packages':inputs,'libubootenvSourceSha256':'95b747e37661bd2e71a1ef34ecddf62af7a42a71459381e2599e8c7aac52770c'},indent=2)+'\n')
reproduce=release/'reproducibility';reproduce.mkdir(exist_ok=True)
for name in ('cross-candidate.sh','cc-cross','assemble.py','validate-cross.py','cross-receipt.py','licenses-sources.py'):
 shutil.copy2(base/name,reproduce/name)
for name in ('swupdate.config','engine-receipt.py','build-engine.sh'):
 shutil.copy2(base/'build-tools'/name,reproduce/name)
(reproduce/'README.txt').write_text('These scripts record the exact isolated developer build paths used on 2026-10-23. They are audit/reproduction evidence, not a printer installer. The existing production native-build guard is unchanged. This release uses cross-validated build metadata and has not been tested on physical printer hardware. The QEMU package checks execute no installation scripts.\n')
print(json.dumps({'binarySha256':receipt['binarySha256'],'buildType':receipt['buildType'],'developmentBuild':receipt['developmentBuild'],'privateLibraries':len(abi['libraries'])-1,'packageChecks':len(checks['tests'])},indent=2))
