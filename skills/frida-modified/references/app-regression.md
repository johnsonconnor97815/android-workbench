# 固定 App 回归

场景：OWASP UnCrackable Level 1 `1.0`，包名 `owasp.mstg.uncrackable1`。APK 不随 Skill 分发。

- 来源：[OWASP/mastg](https://github.com/OWASP/mastg)，`Crackmes/Android/Level_01/UnCrackable-Level1.apk`。
- 固定 Git blob：`9a4f638f1c4a5296fb4eace04b328aecce659b79`。
- 固定下载 API：`https://api.github.com/repos/OWASP/mastg/git/blobs/9a4f638f1c4a5296fb4eace04b328aecce659b79`；解码响应中的 Base64 `content` 后核验 SHA-256。
- SHA-256：`1da8bf57d266109f9a07c01bf7111a1975ce01f190b9d914bcd3ae3dbef96f21`。

先保存样本来源、哈希和 APK 签名检查结果。安装后确认 `pm path` 指向单 APK 且设备文件哈希相同。脚本要求已经安装、可交互的测试 App，不自动安装、解锁有凭据的锁屏或修改签名。

```bash
"$RUN/client/bin/python" "$SKILL/scripts/probe_uncrackable.py" --serial "$SERIAL" \
  --version "$VERSION" --server-pid "$SERVER_PID" --server "$SERVER" --apk "$APK" \
  --rounds 2 --output "$RUN/app-regression"
```

从已核对的部署记录取得 server PID。服务名称和文件路径可以不同，脚本核对运行文件哈希及 PID 身份。自定义端口用 `--server-port`。

Frida 17+ 可显式提供 `--java-bridge`，输入需为暴露 `bridge` 变量的兼容打包脚本；否则检查配套 frida-tools 是否提供 `bridges/java.js`。找不到时记录受阻，不把缺失 bridge 归因于 Android 兼容性。较早版本可使用内置 `Java`，但该案例的其他 API 仍需实际验证。

每轮从新进程开始：QJS spawn、安装启动期 Java hooks、resume，待界面就绪后执行 Java/native/RPC 检查；在相同输入下观察 `Nope... → Success! → 卸载后 Nope...`，随后 V8 reattach，再检查改写与卸载恢复。记录 XML、截图、Hook/RPC 消息和二进制消息。启动期 Java hook 与非启动期 native 检查分开，避免把测试本身安排在不必要的启动竞争窗口。

该 App 的 root 检查在测试脚本中处理。类初始化的特殊处理仅属于该固定场景。`--deoptimize` 是诊断选项，不是默认步骤。所有 probe 成功且无清理错误才返回整体通过；崩溃、无目标界面或脚本已销毁均保留失败证据。

结束时保留 APK 安装，停止本次测试 App，移除专属转发和 UI 临时文件，核对 server 身份与哈希。此场景不代表所有 App、ARM32 Java、APK 内 Gadget 集成或反 Frida 检测效果。
