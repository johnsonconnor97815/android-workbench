# Component provenance and licenses

Android Workbench maintains its scheduler and bundled Skills in this repository. The initial import included local integration changes. Original upstream commits, changed-file records and import hashes remain in `sources.lock.json` as historical provenance; that file is not a synchronization manifest for current code.

| Component | Original repository | License |
| --- | --- | --- |
| android-static-env | https://github.com/johnsonconnor97815/android-static-env | [MIT](licenses/android-static-env/LICENSE) |
| frida-modified | https://github.com/johnsonconnor97815/frida-modified | [MIT and upstream patch exceptions](licenses/frida-modified/LICENSE) |
| pull-android-apk | https://github.com/johnsonconnor97815/pull-android-apk | [MIT](licenses/pull-android-apk/LICENSE) |
| apk-reverse | https://github.com/newliver666/apk-reverse | [MIT](licenses/apk-reverse/LICENSE) |

`analysis-agent` 是本仓库的原生组件。它的请求路由、上下文预算、包证据接口、资源/预览/源码导航和证据契约参考了 ReArk 的交互设计，但没有复制 ReArk 源码，也不包含 ReArk 的 `hyle`、Qt 或 HarmonyOS 依赖。

The Frida patches retain their upstream license, including its exception. See [Frida-COPYING](skills/android-workbench/components/frida/assets/licenses/Frida-COPYING), [Frida-COPYING.LIB](skills/android-workbench/components/frida/assets/licenses/Frida-COPYING.LIB), and the original [component notice](licenses/frida-modified/NOTICE.md). Exact upstream commits and patch hashes are recorded in the profile assets.

No APK samples, Android SDK distribution, Frida binaries or analysis tool environments are included. Tool downloads retain their respective upstream licenses.
