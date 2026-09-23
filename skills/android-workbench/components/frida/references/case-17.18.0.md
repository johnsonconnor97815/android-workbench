# 17.18.0 案例与构建适配

适用源码为 `frida/frida` 的 `17.18.0`，主提交 `5d85d2ced9de2fdce82668f1516f64866c079da4`。core 为 `0602e5dca4c9d7be6098fc319edf978724dabeab`，gum 为 `22e077120358a49b26e32110a6e2b8e80f1ed7f1`。完整检查项见 `assets/profiles/17.18.0/profile.json`。

`COMPONENT` 指包含本文件的 Frida 组件目录；`RUN` 指本次任务目录。

## 修改组

- `libc-stream-init`：core 的 stdio/目录登记表按需初始化，并在构造函数中保留初始化期间已创建的表。原项目中官方 ARM32 Gadget 也在加载时崩溃；修复后 ARM32 Gadget 加载与完整卸载通过。
- `runtime-names`：core/gum 的指定线程、Agent/Gadget SONAME、临时文件、helper/socket、Zymbiote 字段及主机相关代码同步改名。保留 `re.frida.*` 协议、`frida:rpc` 和默认端口。包括 Fruity 相关匹配逻辑的同步，但没有 iOS 运行验证。

这是可审阅的固定案例，不是“替换所有 frida 字符串”的模板，也不承诺绕过任意 App 检测。

## 应用与构建

在已完成原版基线和工具链检查的独立、干净源码树执行：

```bash
PROFILE="$COMPONENT/assets/profiles/17.18.0/profile.json"
python3 "$COMPONENT/scripts/apply_profile.py" --source "$RUN/source" --profile "$PROFILE" \
  --check --record "$RUN/patch-check.json"
python3 "$COMPONENT/scripts/apply_profile.py" --source "$RUN/source" --profile "$PROFILE" \
  --record "$RUN/patch-apply.json"
```

默认应用两组。可用 `--group libc-stream-init` 单独研究兼容性修复；每种组合从独立干净树开始，记录自己的测试范围。命令拒绝错误 commit、dirty 源码和不符的补丁哈希。生成资源在构建阶段处理。

已使用的工具组合：Linux x86_64、NDK `29.0.14206865`、JDK 17、Android SDK API 29 的 `android.jar`、build-tools `36.0.0`。这不是其他版本的统一工具链。配置阶段还会检查/取得上游 SDK、Vala 等资源；保留构建日志。

```bash
python3 "$COMPONENT/scripts/build.py" --source "$RUN/source" --version 17.18.0 \
  --profile "$PROFILE" --patch-record "$RUN/patch-apply.json" \
  --mode android --build-dir "$RUN/build-android" --prefix "$RUN/install-android" \
  --ndk "$NDK" --sdk "$ANDROID_SDK_ROOT" --build-tools 36.0.0 --helper-api 29 \
  --jobs 4 --record "$RUN/build-android.json"
```

`--preflight` 检查工具和输入、生成必要资源但不运行完整配置/构建；`--configure-only` 进一步运行上游配置。无补丁构建省略 `--profile` 和 `--patch-record`，并要求干净源码。配置成功不等于完整构建成功。

`sync_resources.py` 重建 helper DEX，并核对四种预编译 Zymbiote ELF 的确切输入哈希，只更新已知的 64 字节 socket 字段，验证结果哈希及长度。这个字段布局仅适用于本案例。

构建目录和安装目录放在源码树外。目录复用须有匹配的输入记录；参数、源码或工具链变化时使用新目录。Android 输出 server 以及 ARM64/ARM32 Agent、Gadget，产物和哈希列在构建记录中。

主机 Python 客户端使用相同 source/profile/patch-record 参数，把 `--mode` 改为 `host`，并使用独立 `build-host`、`install-host` 和记录路径。需要在构建 Python 环境中准备 `pip`、`setuptools`、`wheel`；输出 wheel 安装到本次任务的客户端 venv，之后记录 `pip freeze`。客户端版本来自 `--version`。

## 证据范围

原案例为 Pixel 3 / Android 12 / API 31 / ARM64。已有 ARM32/ARM64 native attach/spawn、Gadget harness 加载/完整卸载，以及固定 OWASP UnCrackable Level 1 的 QJS spawn、V8 reattach、Java/native Hook、RPC 和卸载恢复记录。

原项目 App 测试在两个新进程中共完成 102 个断言，固定输入观察到 `Nope... → Success! → 卸载后 Nope...`。此处为历史证据摘要；组件脚本迁移后的验证范围和结果见仓库发布记录。原始设备序列号、完整本机路径及 APK 不随插件分发。

对该 App，启动期 hook 曾需要显式初始化特定 Java 类才稳定命中；该处理留在场景脚本中，不作为所有 Java hook 的前置条件。尚未验证 Android 10、ARM32 Java、APK 内 Gadget 集成或任意 App 的反 Frida 检测效果。
