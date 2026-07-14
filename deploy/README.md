# 部署到 Google Compute Engine（常驻纸面 dry run）

paper dry run 是个 7×24 常驻引擎（挂实时 WebSocket、跑信号循环、写账本），不适合 Cloud Run / Cloud Functions 那种无服务器（无请求就缩到零、文件系统临时、要求监听 $PORT）。用一台小 VM 最省事：现有代码原样跑，持久磁盘存账本。`e2-micro` 有永久免费额度，这负载（大部分时间挂着等 WebSocket）绰绰有余。

## 1. 建 VM（浏览器，GCP 控制台）

Compute Engine → 虚拟机实例 → 创建：

- **区域**：`us-central1`（或 `us-west1` / `us-east1`）——`e2-micro` 免费额度只在这三个区
- **机器类型**：`e2-micro`
- **启动磁盘**：Debian 12 或 Ubuntu 22.04，标准永久磁盘 10 GB 够了
- 防火墙不用勾 HTTP/HTTPS（引擎只往外连，不收入站请求）
- 创建

## 2. 跑部署脚本（VM 的 SSH 里）

点实例的 **SSH** 按钮进终端，跑：

```bash
curl -fsSL https://raw.githubusercontent.com/DonnyDing1999/polyCopyCat/main/deploy/setup.sh | bash
```

（脚本内容可先在 GitHub 上看：`deploy/setup.sh`）。它会装依赖、拉代码、建 venv，并**做连通性自检**。

⚠️ **重点看自检里 CLOB 那行**：

- `CLOB: ✓` → 云机能连 CLOB，继续第 3 步
- `CLOB: ✗ 被拦截` → 云厂商出口 IP 被 Polymarket 的 Cloudflare 防护挡了。换个区域重建 VM 再试，或挂代理；不通就下不了单

## 3. 提供两样东西

**配置**（含你的 7 个跟单目标，不在仓库里）——本地机器上传：

```bash
gcloud compute scp paper-week.json <VM名>:~/polyCopyCat/paper-week.json
```

**Discord webhook**——VM 上写进环境文件：

```bash
echo 'POLYCOPYCAT_DISCORD_WEBHOOK=<你的webhook地址>' > ~/polyCopyCat/deploy.env
```

## 4. 启动

```bash
sudo systemctl enable --now polycopycat     # 启动 + 开机自启
journalctl -u polycopycat -f                # 看实时日志
```

日志里应看到「启动自检」通过、「实时成交流已连接」，之后目标一有新成交就跟单、并推到 Discord 频道。

## 运维速查

| 需求 | 命令 |
|---|---|
| 看日志 | `journalctl -u polycopycat -f` |
| 看战绩 | `cd ~/polyCopyCat && .venv/bin/python -m polycopycat report --config paper-week.json --by-target` |
| 改配置后重启 | `sudo systemctl restart polycopycat` |
| 更新代码 | `git -C ~/polyCopyCat pull && sudo systemctl restart polycopycat` |
| 停 | `sudo systemctl stop polycopycat` |
| 急停不下新单 | `touch ~/polyCopyCat/STOP`（删掉恢复）|

VM 一直开着即可，不受你本地电脑关机影响。跑一周后 `report --by-target` 看谁值得真金白银跟。
