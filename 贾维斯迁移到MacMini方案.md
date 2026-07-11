# 贾维斯迁移到 Mac mini 方案

> 场景：从**旧 Mac** 迁到 **Mac mini（Apple Silicon）**，作为持久运行环境。
> 范围：代码 + 数据 + 密钥迁移 + launchd 常驻。公网暴露沿用现状，单独按 `jarvis前端暴露迁移方案.md` 另行处理。
> 生成日期：2026-07-10

---

## 0. 头号风险：加密密钥（先看这个，否则数据搬过去打不开）

`core/memory.py` 的密钥解析顺序是 **先读 macOS 钥匙串，再回退 `.env`**：

```python
key = keyring.get_password("jarvis", "memory_encryption_key")   # ① 钥匙串优先
if not key:
    key = config.MEMORY_ENCRYPTION_KEY or None                  # ② 再回退 .env
if not key:
    key = Fernet.generate_key(); keyring.set_password(...)      # ③ 都没有→新生成
```

后果：旧 Mac 首次运行时把自动生成的密钥存进了**钥匙串**，`memory.db`（长期记忆、证件保险箱、保单）就是用**那把**加密的。`.env` 里那把未必相同。如果只搬 `.env` 和 `memory.db`、不管钥匙串，新机是空钥匙串 → 走 ②用 `.env` 的 key → 若两把不一致，**解密直接失败，记忆和保险箱全部读不出**。

**所以第一步一定是把旧机真正在用的那把 key 取出来，并让新机用同一把。**

在旧 Mac 上取出钥匙串里的密钥并和 `.env` 比对：

```bash
# 钥匙串里的真实密钥（会弹窗要授权，点“始终允许”）
security find-generic-password -s jarvis -a memory_encryption_key -w

# .env 里的那把
grep MEMORY_ENCRYPTION_KEY /path/to/jarvis/.env
```

- 两者**一致** → 省心，`.env` 就是权威，直接搬 `.env` 即可。
- 两者**不一致，或钥匙串取到值而 `.env` 为空** → **以钥匙串那把为准**，记下来，第 4 步写进新机 `.env`。
- `security` 命令报 `could not be found` → 说明旧机当初就是用 `.env` 的 key（②路径），`.env` 即权威。

> 建议统一策略：**把权威密钥固定写进新机 `.env`，让新机钥匙串保持为空。** 这样走 ② 路径，避免 launchd 无 GUI 会话时钥匙串被锁、headless 读不到 key 的坑（见 §5 注意）。

---

## 1. 迁移前盘点（旧 Mac）

要搬的东西分三类：

**A. 代码仓库** —— 整个 `jarvis/` 目录（走 git 最干净）。
排除 `.venv/`、`__pycache__/`、`.DS_Store`、`better jarvis/`（看着像旧备份，确认后不带）。

**B. 运行时状态（真正的“大脑”，务必带全）**

| 内容 | 位置 | 说明 |
|---|---|---|
| 加密数据库 | `~/Library/Application Support/Jarvis/memory.db` | 记忆+保险箱+保单，**权威副本在这，不是仓库里的 data/**|
| WAL 边车文件 | 同目录 `memory.db-wal` / `memory.db-shm` / `-journal` | 代码用 WAL，**必须连边车一起拷，或先 checkpoint** |
| 潜客树 | `~/Library/Application Support/Jarvis/prospect_tree.json` | 若存在 |
| 加密密钥 | 钥匙串 / `.env`（见 §0） | 决定 memory.db 能否解开 |

> 注意：`config.py` 里 `DATA_DIR` 在 macOS 固定为 `~/Library/Application Support/Jarvis/`。仓库里的 `data/memory.db` 是早期从仓库目录跑时留下的，**不是**当前权威数据，别搞混。迁移前用下面命令确认哪个是活跃库：
> ```bash
> ls -la ~/Library/Application\ Support/Jarvis/
> ```

**C. 配置与自建资产**

- `.env`（含 `OPENROUTER_API_KEY` + 权威加密 key）
- `schedules/`（已激活的定时任务）
- `skills/`（自建技能，如 `weather_query`）
- `deploy/cloudflared.yml`（若暂时继续用 Cloudflare 隧道）

**迁移前把旧机服务停掉**，保证 SQLite 落盘一致，并做一次 WAL checkpoint：

```bash
# 停掉旧 jarvis 进程后，把 WAL 合并回主库，之后拷贝最干净
sqlite3 ~/Library/Application\ Support/Jarvis/memory.db "PRAGMA wal_checkpoint(TRUNCATE);"
```

---

## 2. Mac mini 基础环境

```bash
# 1) Homebrew（Apple Silicon 装在 /opt/homebrew）
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 2) Python 与 git（项目要求 >=3.10，用 3.11 对齐 Dockerfile）
brew install python@3.11 git sqlite

# 3) 关掉 Mac mini 休眠，保证“持久运行”（关键！否则合盖/待机会断服务）
sudo pmset -a sleep 0 disksleep 0
sudo pmset -a womp 1        # 网络唤醒（可选）
# 若希望断电后自动开机、开机自动登录到能跑 LaunchAgent 的用户会话，也在
# 系统设置→节能/电池→“断电后自动重启”里打开。
```

---

## 3. 搬代码

```bash
# 方式一：走 git（推荐）。旧机先 commit 干净，然后：
git clone <你的远端或旧机路径> ~/jarvis
# 或旧机没远端时，直接 rsync 仓库（排除虚拟环境和缓存）：
rsync -av --exclude '.venv' --exclude '__pycache__' --exclude '.DS_Store' \
  旧机:/path/to/jarvis/ ~/jarvis/
```

建虚拟环境并装依赖（Apple Silicon 上 `rapidocr-onnxruntime`/`opencv`/`onnxruntime` 都有 arm64 轮子，正常即可）：

```bash
cd ~/jarvis
/opt/homebrew/bin/python3.11 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .                 # 核心依赖（pyproject）
pip install -e '.[documents,ocr]'  # 文档解析 + 证件OCR（按需）
# 若用潜客/PDF日报流程，再装 playwright：
# pip install playwright && playwright install chromium
```

> 如遇个别包无 arm64 轮子而要本地编译，先 `xcode-select --install` 装命令行工具即可。

---

## 4. 搬数据与密钥

```bash
# 1) 建数据目录并拷贝运行时状态（连 WAL 边车一起）
mkdir -p ~/Library/Application\ Support/Jarvis
rsync -av 旧机:~/Library/Application\ Support/Jarvis/ \
         ~/Library/Application\ Support/Jarvis/

# 2) 拷 .env（含 API key + 权威加密 key）
scp 旧机:/path/to/jarvis/.env ~/jarvis/.env

# 3) 拷自建资产
rsync -av 旧机:/path/to/jarvis/schedules/ ~/jarvis/schedules/
rsync -av 旧机:/path/to/jarvis/skills/    ~/jarvis/skills/
```

**按 §0 结论确认密钥就位**：把权威密钥写进 `~/jarvis/.env` 的 `MEMORY_ENCRYPTION_KEY=`，并确保新机钥匙串里没有 `jarvis / memory_encryption_key` 这条（保持空，让程序走 `.env`）。查/删钥匙串条目：

```bash
security find-generic-password -s jarvis -a memory_encryption_key -w 2>/dev/null \
  && echo "钥匙串里有旧值，若和 .env 不一致要删：" \
  && echo "security delete-generic-password -s jarvis -a memory_encryption_key"
```

**先手动验一次再上常驻**：

```bash
cd ~/jarvis && source .venv/bin/activate
python main.py
# 浏览器开 http://localhost:8000，验证：
#  - 能对话
#  - “我有哪些证件/保单” 能读出保险箱（== memory.db 解密成功）
#  - 历史记忆在
#  - 定时任务列表在
# 一切正常再 Ctrl+C，进入 §5 做常驻。
```

---

## 5. launchd 持久运行（本次核心）

用 **LaunchAgent**（用户级，能读该用户钥匙串/会话；比 LaunchDaemon 更适合本场景）。

创建 `~/Library/LaunchAgents/com.ned.jarvis.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>            <string>com.ned.jarvis</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/你的用户名/jarvis/.venv/bin/python</string>
        <string>/Users/你的用户名/jarvis/main.py</string>
    </array>
    <key>WorkingDirectory</key> <string>/Users/你的用户名/jarvis</string>
    <key>RunAtLoad</key>        <true/>
    <key>KeepAlive</key>        <true/>              <!-- 崩溃自动拉起 -->
    <key>StandardOutPath</key>  <string>/Users/你的用户名/jarvis/logs/jarvis.out.log</string>
    <key>StandardErrorPath</key><string>/Users/你的用户名/jarvis/logs/jarvis.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>JARVIS_HOST</key> <string>127.0.0.1</string>
        <key>JARVIS_PORT</key> <string>8000</string>
    </dict>
</dict>
</plist>
```

加载并自检：

```bash
mkdir -p ~/jarvis/logs
launchctl load  ~/Library/LaunchAgents/com.ned.jarvis.plist
launchctl list | grep jarvis          # 看到 PID 即已跑
tail -f ~/jarvis/logs/jarvis.err.log  # 看启动日志
# 停 / 重载：
# launchctl unload ~/Library/LaunchAgents/com.ned.jarvis.plist
```

**注意（headless 与钥匙串）**：LaunchAgent 需要该用户**已登录的图形会话**才最稳（钥匙串解锁、`open` 等可用）。因此建议 Mac mini 开机自动登录到你的账户。既然 §0/§4 已把加密 key 固定进 `.env`、钥匙串留空，即便钥匙串被锁，程序也走 `.env` 读 key，不受影响——这正是选 `.env` 策略的原因。

> 若坚持要真正无人登录也能跑（LaunchDaemon / 系统级），钥匙串会读不到、`.env` 方案更是必须；且数据目录 `~/Library/Application Support/Jarvis` 要相应调整为守护进程可访问的路径。本次场景不需要，保持 LaunchAgent + 自动登录即可。

---

## 6. 公网暴露（本次只做占位，沿用现状）

当前 `serve.sh` 用 Cloudflare 隧道（`deploy/cloudflared.yml`，域名 `jarvis.hks0110.com`）。迁到 Mac mini 后临时继续用它最省事：在 mini 上装 `cloudflared`、拷贝隧道凭证与 `cloudflared.yml`，单独再起一个 LaunchAgent 跑隧道即可。

> 你记忆里已定的 **弃 CF → 阿里云 + frp + 非标端口 + Caddy + mTLS** 自建方案属于独立工程，见仓库 `jarvis前端暴露迁移方案.md`，等本次迁移稳定后单独实施，不在本方案范围内。

---

## 7. 验收清单

- [ ] `launchctl list | grep jarvis` 有 PID，`http://localhost:8000` 可访问
- [ ] 保险箱可读（证件/保单列得出来）→ 证明 memory.db 用对了 key
- [ ] 历史对话/长期记忆在
- [ ] 定时任务（schedules）已加载、按时触发
- [ ] 自建技能（skills）可用
- [ ] 重启 Mac mini 后服务自动起来（RunAtLoad 生效）
- [ ] `kill` 掉进程后能被 KeepAlive 自动拉起
- [ ] 休眠已关（`pmset -g | grep sleep` 为 0）
- [ ] 公网入口可达（沿用 Cloudflare 或后续 frp）

## 8. 回滚

旧 Mac 上的仓库、`~/Library/Application Support/Jarvis/` 和钥匙串**先原样保留至少一两周**，别急着删。新机跑稳、验收全过后再退役旧机。数据是加密单库 `memory.db`，回滚只需把旧机重新启动服务即可。
