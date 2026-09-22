SignalLens Windows Portable
===========================

1. 解压整个 ZIP（不要直接在压缩包内运行）
2. 双击 SignalLens.exe
3. 浏览器会自动打开 http://127.0.0.1:8765
4. 第一次运行需要填写 TypeSafe API Key
5. 所有本地数据都在同目录的 data 文件夹里
6. 关闭 SignalLens 窗口即可停止程序

注意事项
--------

* 请把文件夹放在可写位置（桌面、文档等）。
  不要放进 Program Files，也不要直接从 ZIP 里运行，
  否则 SignalLens 无法保存数据。
* SignalLens 只监听本机 127.0.0.1，不会对外开放，
  也不会收集使用统计。
* 升级方法：先复制一份 data 文件夹作为备份，再用新版本覆盖
  程序文件，保留原来的 data 文件夹即可（API Key 与分析缓存
  都保存在那里）。

文件说明
--------

SignalLens.exe     程序入口（双击运行）
_internal/         程序运行时文件，不需要手动改动
data/              你的本地数据（API Key、分析缓存、日志）
LICENSE            开源许可
VERSION            版本号

Windows SmartScreen 提示
------------------------

SignalLens 是第三方未签名开源程序。首次运行时 Windows 可能显示
"Windows 已保护你的电脑"（SmartScreen）提示：点"更多信息" →
"仍要运行"即可。可在 GitHub Releases 页面用页面提供的 SHA256
校验 ZIP 完整性。

数据与隐私
----------

* API Key 只保存在本机 data/settings.env，不会上传、不会写进
  报告或日志。
* 聊天内容在本地脱敏后，仅把目标消息与最多 5 条上下文发送给
  TypeSafe Jev；分析缓存、报告导出都在本地完成。
* 删除 data 文件夹即可完全清除 SignalLens 在本机的所有数据。
