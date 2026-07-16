#!/usr/bin/env bash
# polyCopyCat 一键部署到常驻主机（Google Compute Engine e2-micro，或任意 Debian/Ubuntu）。
#
# 在目标机器上跑：
#   curl -fsSL https://raw.githubusercontent.com/DonnyDing1999/polyCopyCat/main/deploy/setup.sh | bash
# 它会：装依赖 → 拉代码 → 建 venv → 连通性自检（关键：确认云机能连 CLOB）→ 装 systemd 服务。
# 装完还差「配置」和「Discord webhook」两样你来提供，脚本末尾有提示。
set -euo pipefail

REPO_URL="https://github.com/DonnyDing1999/polyCopyCat.git"
APP_DIR="${POLYCOPYCAT_DIR:-$HOME/polyCopyCat}"
RUN_USER="$(id -un)"
CONFIG="$APP_DIR/paper-week.json"
ENVFILE="$APP_DIR/deploy.env"

echo "== 1/5 安装系统依赖 =="
sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip git

echo "== 2/5 拉取代码到 $APP_DIR =="
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" pull --ff-only
else
    git clone "$REPO_URL" "$APP_DIR"
fi

echo "== 3/5 建虚拟环境并安装 =="
cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/pip install -q -U pip
.venv/bin/pip install -q -e .
.venv/bin/pip install -q truststore   # 万一网络有 TLS 拦截；干净网络下无副作用

echo "== 4/5 连通性自检（云机能否连上依赖接口）=="
.venv/bin/python - <<'PY'
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass
from polycopycat.data_api import DataApiClient
from polycopycat.engine.clob import ClobReadClient
for name, probe in [("Data API", DataApiClient), ("CLOB", ClobReadClient)]:
    ok, msg = probe().ping()
    print(f"  {name:9s}: {'✓' if ok else '✗'} {msg}")
print("  ↑ CLOB 必须为 ✓，否则引擎无法下单——换区域/加代理，或换台机器再试。")
PY

echo "== 5/5 安装 systemd 服务 =="
sudo tee /etc/systemd/system/polycopycat.service >/dev/null <<UNIT
[Unit]
Description=polyCopyCat paper dry run
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=-$ENVFILE
ExecStart=$APP_DIR/.venv/bin/python -m polycopycat run --config $CONFIG
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload

echo "== 6/6 安装每日账本备份 timer =="
chmod +x "$APP_DIR/deploy/backup.sh"
sudo tee /etc/systemd/system/polycopycat-backup.service >/dev/null <<UNIT
[Unit]
Description=polyCopyCat ledger daily backup

[Service]
Type=oneshot
User=$RUN_USER
EnvironmentFile=-$ENVFILE
Environment=POLYCOPYCAT_DIR=$APP_DIR
ExecStart=$APP_DIR/deploy/backup.sh
UNIT
sudo tee /etc/systemd/system/polycopycat-backup.timer >/dev/null <<UNIT
[Unit]
Description=polyCopyCat ledger daily backup timer

[Timer]
OnCalendar=*-*-* 04:30:00
RandomizedDelaySec=15m
Persistent=true

[Install]
WantedBy=timers.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now polycopycat-backup.timer

cat <<NEXT

────────────────────────────────────────────────────────
部署骨架就绪。还差两样你来提供，然后启动：

  1) 配置文件（含你的 7 个跟单目标，不在仓库里）→ $CONFIG
     在本地机器上传：
       gcloud compute scp paper-week.json <VM名>:$CONFIG

  2) Discord 频道 webhook → 写进 $ENVFILE：
       echo 'POLYCOPYCAT_DISCORD_WEBHOOK=<你的webhook地址>' > $ENVFILE
     （$ENVFILE 已被 .gitignore，不会进仓库）

两样齐了，启动并设开机自启：
  sudo systemctl enable --now polycopycat
  journalctl -u polycopycat -f
     ↑ 应看到「启动自检」通过 +「实时成交流已连接」

日常运维：
  sudo systemctl restart polycopycat     # 改了配置后重启
  sudo systemctl stop polycopycat        # 停
  git -C $APP_DIR pull && sudo systemctl restart polycopycat   # 更新代码
────────────────────────────────────────────────────────
NEXT
