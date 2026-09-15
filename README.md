# WeChat Monitor

一个仅在 Windows 本机运行的微信聊天记录增量采集工具。它从你自己的本地微信数据目录读取新消息，归档到本地 SQLite，并可将事件推送至你配置的 Webhook。

## 隐私与安全

- 本仓库不包含聊天记录、联系人或群聊名单、本机路径、账号标识、数据库密钥、Webhook 地址或令牌。
- 请只处理你拥有合法权限访问的微信数据。
- `config.json`、`collect_list.txt`、密钥缓存、归档数据库、日志和收集到的附件均已列入 `.gitignore`，不会被 Git 跟踪。

## 安装

需要 Windows、Python 3.11+，以及已安装并登录的微信桌面版。

```powershell
python -m venv .venv
.venv\\Scripts\\Activate.ps1
pip install -r requirements.txt
Copy-Item config.example.json config.json
Copy-Item collect_list.example.txt collect_list.txt
```

编辑本机的 `config.json`，至少填写：

- `wechat_data_dir`：你本机微信的 `xwechat_files` 数据目录。
- `weixin_path`：你本机 `Weixin.exe` 的路径。
- `webhook_urls` 与 `bearer_token`：如需推送时才填写。

编辑本机的 `collect_list.txt`，仅列出你授权采集的会话。

## 使用

首次运行前，先退出微信并提取本机数据库密钥：

```powershell
python main.py --extract-key
```

然后重新登录微信并启动监控：

```powershell
python main.py
```

密钥只会保存在本机的 `password.key`；不要将它上传或分享。
