function exportAddress(moduleName, name) {
  const module = Process.getModuleByName(moduleName);
  if (typeof module.getExportByName === 'function') return module.getExportByName(name);
  if (typeof Module.getExportByName === 'function') return Module.getExportByName(moduleName, name);
  throw new Error('PROBE_UNSUPPORTED: no compatible module export API');
}
function readText(path) {
  if (typeof File.readAllText === 'function') return File.readAllText(path);
  const file = new File(path, 'r');
  try {
    if (typeof file.readText !== 'function') throw new Error('PROBE_UNSUPPORTED: no compatible File API');
    return file.readText();
  } finally { file.close(); }
}
let hits = 0;
const address = exportAddress('libc.so', 'getpid');
const getpid = new NativeFunction(address, 'int', []);
const hook = Interceptor.attach(address, { onEnter() { hits++; } });
Interceptor.flush();
rpc.exports = {
  probe() {
    const nativePid = getpid();
    return { pid: Process.id, arch: Process.arch, runtime: Script.runtime,
             nativePid, hits, statusBytes: readText('/proc/self/status').length };
  }
};
