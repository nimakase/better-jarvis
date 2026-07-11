# 部署 / 公网访问（Cloudflare Tunnel）

隧道配置与项目绑定在这里，作为**单一事实来源**。

## 文件

- `cloudflared.yml` — 隧道配置（协议、路由、稳定性参数）。可入库，无密钥。
- 凭证 `~/.cloudflared/<隧道ID>.json` — **密钥，不入库**，由 `cloudflared.yml` 以绝对路径引用。

当前隧道：`jarvis`（ID `daa886d2-8350-4e29-9a50-ef0cb27d21e4`），域名 `jarvis.hks0110.com`。

## 日常启动（手动）

项目根目录下：

```bash
./serve.sh
```

会同时拉起后端（`main.py`）和隧道，`Ctrl+C` 一起关。

## 只单独跑隧道

```bash
cloudflared tunnel --config deploy/cloudflared.yml run jarvis
```

## 让系统服务也读这份配置（开机自启）

`cloudflared service install` 默认读 `~/.cloudflared/config.yml`。把它软链到项目这份，
即可保持单一事实来源——以后只改 `deploy/cloudflared.yml`：

```bash
ln -sf "$(pwd)/deploy/cloudflared.yml" ~/.cloudflared/config.yml
sudo cloudflared service install        # 装成开机自启服务
# 卸载：sudo cloudflared service uninstall
```

## 当服务器：关屏不休眠（需插电源）

```bash
sudo pmset -c sleep 0           # 接电源时系统永不休眠
sudo pmset -c displaysleep 5    # 屏幕 5 分钟后关闭（关屏≠休眠）
# 想合盖也跑：sudo pmset -c disablesleep 1（散热差，慎用）
pmset -g                        # 查看当前设置
```

## 稳定性参数说明（cloudflared.yml）

- `protocol: http2` — 走 TCP 443，避免家用网络掐空闲 UDP 导致 QUIC 反复重连。
- `edge-ip-version: 4` — 锁 IPv4 边缘，绕开不稳的 IPv6。
- `retries / grace-period` — 断连后的重试与优雅退出窗口。

## 安全提醒

公网暴露后务必在 Cloudflare Zero Trust → Access 加邮箱登录门，只放行本人邮箱。
jarvis 能读写本机文件、含证件保险箱，**切勿裸暴露**。
