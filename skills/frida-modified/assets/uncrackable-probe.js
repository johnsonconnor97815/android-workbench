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
// This fixture uses a bundled bridge on Frida 17+ or the built-in bridge on older versions.
const J = typeof bridge !== 'undefined' ? bridge : (typeof Java !== 'undefined' ? Java : null);
if (J === null) throw new Error('PROBE_UNSUPPORTED: Java bridge not provided');
const state = {
  pid: Process.id, arch: Process.arch, runtime: Script.runtime,
  javaAvailable: J.available, ready: false, rewrite: false,
  onCreateHits: 0, rootHits: 0, verifyCalls: [], cryptoCalls: 0
};
let Checker;

function inJava(fn) {
  return new Promise((resolve, reject) => {
    J.perform(() => {
      try { resolve(fn()); } catch (error) { reject(error); }
    });
  });
}

const initialized = inJava(() => {
  if (!J.available) throw new Error('Java is not available');
  state.initializationBacktrace = J.backtrace({ limit: 24 }).frames.map(frame => frame.signature);
  if (TEST_OPTIONS.deoptimize) {
    J.deoptimizeEverything();
    state.deoptimized = true;
  }
  const Class = J.use('java.lang.Class');
  for (const name of ['sg.vantagepoint.uncrackable1.MainActivity',
                      'sg.vantagepoint.uncrackable1.a', 'sg.vantagepoint.a.a']) {
    Class.forName.overload('java.lang.String', 'boolean', 'java.lang.ClassLoader')
        .call(Class, name, true, J.classFactory.loader);
  }
  state.classesInitialized = true;
  // Install the entry hook before resolving and invoking the check methods.
  if (TEST_OPTIONS.hooks) {
    const activity = J.use('sg.vantagepoint.uncrackable1.MainActivity');
    const onCreate = activity.onCreate.overload('android.os.Bundle');
    onCreate.implementation = function (bundle) {
      state.onCreateHits++;
      send({ event: 'onCreate', pid: Process.id });
      return onCreate.call(this, bundle);
    };
  }
  const RootCheck = J.use('sg.vantagepoint.a.c');
  Checker = J.use('sg.vantagepoint.uncrackable1.a');
  state.rootOriginal = {};
  for (const name of ['a', 'b', 'c']) {
    const method = RootCheck[name].overload();
    state.rootOriginal[name] = method.call(RootCheck);
    if (TEST_OPTIONS.hooks) {
      method.implementation = function () {
        if (state.rootHits === 0)
          state.firstRootBacktrace = J.backtrace({ limit: 24 }).frames.map(frame => frame.signature);
        state.rootHits++;
        return false;
      };
    }
  }
  if (TEST_OPTIONS.hooks) {
    const check = Checker.a.overload('java.lang.String');
    check.implementation = function (input) {
      const original = check.call(this, input);
      const result = state.rewrite ? true : original;
      const call = { input: String(input), original, result, rewrite: state.rewrite };
      state.verifyCalls.push(call);
      send({ event: 'verify', ...call });
      return result;
    };
    const decrypt = J.use('sg.vantagepoint.a.a').a.overload('[B', '[B');
    decrypt.implementation = function (key, data) {
      state.cryptoCalls++;
      return decrypt.call(this, key, data);
    };
  }
  state.ready = true;
  send({ event: 'ready', pid: Process.id, hooks: TEST_OPTIONS.hooks });
  return state;
});
// Report initialization failures even when the host is still waiting for resume.
initialized.catch(error => send({ event: 'initialization-error', error: String(error) }));

rpc.exports = {
  async ready() { await initialized; return state; },
  snapshot() { return state; },
  setrewrite(enabled) {
    if (!TEST_OPTIONS.hooks) throw new Error('Hooks are disabled');
    state.rewrite = Boolean(enabled);
    return state.rewrite;
  },
  async setinput(input) {
    await initialized;
    return new Promise((resolve, reject) => {
      J.perform(() => {
        let activity = null;
        J.choose('sg.vantagepoint.uncrackable1.MainActivity', {
          onMatch(instance) { activity = J.retain(instance); return 'stop'; },
          onComplete() {
            if (activity === null) { reject(new Error('No live MainActivity')); return; }
            J.scheduleOnMainThread(() => {
              try {
                const id = activity.getResources().getIdentifier('edit_text', 'id',
                                                                 activity.getPackageName());
                const edit = J.cast(activity.findViewById(id), J.use('android.widget.EditText'));
                const value = J.use('java.lang.String').$new(input);
                const editable = J.use('android.widget.TextView$BufferType').EDITABLE.value;
                edit.setText.overload('java.lang.CharSequence', 'android.widget.TextView$BufferType')
                    .call(edit, value, editable);
                const text = J.cast(edit.getText(), J.use('java.lang.CharSequence'));
                resolve(text.toString());
              } catch (error) { reject(error); }
              finally { activity.$dispose(); }
            });
          }
        });
      });
    });
  },
  async javaprobe(input) {
    await initialized;
    return inJava(() => {
      const context = J.use('android.app.ActivityThread').currentApplication();
      const classes = J.enumerateLoadedClassesSync();
      return {
        package: context.getPackageName().toString(),
        androidVersion: J.androidVersion,
        originalResult: Checker.a.overload('java.lang.String').call(Checker, input),
        loadedClassCount: classes.length,
        checkerClassFound: classes.includes('sg.vantagepoint.uncrackable1.a'),
        activityClassFound: classes.includes('sg.vantagepoint.uncrackable1.MainActivity'),
        roots: state.rootOriginal
      };
    });
  },
  nativeprobe() {
    if (typeof File.readAllBytes !== 'function') throw new Error('PROBE_UNSUPPORTED: this App fixture needs File.readAllBytes');
    const libc = Process.getModuleByName('libc.so');
    const pidAddress = exportAddress('libc.so', 'getpid');
    const getpid = new NativeFunction(pidAddress, 'int', []);
    let pidHits = 0;
    const pidHook = Interceptor.attach(pidAddress, { onEnter() { pidHits++; } });
    let nativePid;
    try { Interceptor.flush(); nativePid = getpid(); }
    finally { pidHook.detach(); Interceptor.flush(); }

    const text = 'frida-functional-probe';
    const buffer = Memory.allocUtf8String(text);
    const strlenAddress = exportAddress('libc.so', 'strlen');
    const strlen = new NativeFunction(strlenAddress, 'ulong', ['pointer']);
    const before = Number(strlen(buffer).toString());
    let strlenHits = 0;
    const lengthHook = Interceptor.attach(strlenAddress, {
      onEnter(args) { this.ownedBuffer = args[0].equals(buffer); },
      onLeave(value) {
        if (this.ownedBuffer) { strlenHits++; value.replace(text.length + 7); }
      }
    });
    let hooked;
    try { Interceptor.flush(); hooked = Number(strlen(buffer).toString()); }
    finally { lengthHook.detach(); Interceptor.flush(); }
    const restored = Number(strlen(buffer).toString());

    const memory = Memory.alloc(5);
    memory.writeByteArray([0xde, 0xad, 0xbe, 0xef, 1]);
    const bytes = memory.readByteArray(5);
    send({ event: 'binary-probe', pid: Process.id }, bytes);
    // procfs command lines are NUL-delimited bytes, not a text file.
    const cmdline = new Uint8Array(File.readAllBytes('/proc/self/cmdline'));
    const end = cmdline.indexOf(0);
    const command = String.fromCharCode(...cmdline.subarray(0, end < 0 ? cmdline.length : end));
    return {
      pid: Process.id, nativePid, pidHits, arch: Process.arch,
      strlen: { before, hooked, restored, hits: strlenHits, expected: text.length },
      memory: Array.from(new Uint8Array(bytes)),
      fileCmdline: command,
      statusBytes: readText('/proc/self/status').length,
      libc: libc.name,
      artFound: Process.findModuleByName('libart.so') !== null
    };
  }
};
