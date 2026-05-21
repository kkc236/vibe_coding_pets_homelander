# AI Finish Pet

一个 Windows 透明小像素祖国人桌宠，用来在网页 AI 回答完成时发出桌面提醒。

## 当前行为

- 桌宠以透明小像素祖国人形象显示在桌面上，包含金发、蓝色战衣、红披风、金色肩甲等特征。
- 按住左键可拖动桌宠，拖动时会切换为飞行动画；向左拖和向右拖会自动切换飞行朝向。
- 右键打开菜单，包含：测试提醒、复制监听脚本、声音、红屏、眼射激光、Codex 完成提醒、强度、退出。
- 触发提醒时可播放更长的警报音；开启激光时会从眼睛发出光束并逐渐覆盖全屏。
- Chrome 扩展仍可监听 ChatGPT / Claude / Gemini 等页面的回答完成状态，并通知桌宠。
- 桌宠会在后台监听本机 Codex Desktop 日志：启动时会同步当前是否已有 Codex 回合在运行，之后实时监听 `turn/start` 和 `turn-complete` 事件。

## 启动

推荐双击运行：

```powershell
Start-AIFinishPet.vbs
```

`Start-AIFinishPet.vbs` 会隐藏 PowerShell 窗口；`Start-AIFinishPet.ps1` 会优先使用 `pythonw.exe`，避免留下控制台窗口。也可以直接运行：

```powershell
python .\assistant_pet.py
```

## Chrome 扩展监听

1. 启动桌宠。
2. 打开 Chrome 扩展管理页：`chrome://extensions/`
3. 开启“开发者模式”。
4. 点击“加载已解压的扩展程序”。
5. 选择项目内的 `chrome-extension` 文件夹。

安装后，打开支持的 AI 聊天页面，扩展会监听回答完成并通知桌宠。

## 控制台脚本监听

如果不安装扩展，也可以用右键菜单里的“复制监听脚本”临时接入：

1. 启动桌宠。
2. 右键桌宠，选择“复制监听脚本”。
3. 打开 AI 聊天页面，按 `F12` 进入开发者工具的 Console。
4. 粘贴脚本并回车。

该方式适合临时测试；长期使用建议安装 Chrome 扩展。

## 安全边界

- 只监听本机 `127.0.0.1:8765`。
- `/notify` 需要本地生成的 token。
- 红屏会自动关闭，也可以按 `Esc` 关闭。
- 不做全局键盘锁定，不阻止用户操作。
