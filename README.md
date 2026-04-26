# 德州扑克训练器

基于 GPT 的多人德州扑克训练 Web 应用。AI 对手具有真实打牌风格，并提供实时操作建议与赛后牌局分析。

---

## 功能

- **多 AI 对手**：2~5 名，每人可独立配置风格（紧凶 TAG / 松凶 LAG / 紧弱 / 松弱 / 均衡 GTO / 随机）
- **实时提示**：点击"💡 建议"，AI 教练即时给出推荐动作、加注额、置信度及分析
- **赛后分析**：每手结束后 AI 对本手牌局打分并给出改进建议
- **双次发牌（Run-It-Twice）**：双方全押时可选择是否发两次以降低方差，AI 对手同样会做出决策
- **可视化牌桌**：椭圆形牌桌，玩家座位圆弧分布，当前行动者高亮
- **游戏配置**：所有参数均可在浏览器配置弹窗中调整，无需重启服务器
- **筹码调整**：可实时修改玩家和 AI 对手的当前筹码
- **筹码上限（max_chips）**：每手开始前自动卸码，防止单一玩家筹码差距过大
- **数据统计**：盈亏记录、胜率、行动记录，持久化在浏览器 localStorage
- **密码保护**：可选，支持多密码
- **响应式界面**：宽屏完整显示，窄屏侧边栏折叠为弹出面板

---

## 技术栈

| 层 | 技术 |
|----|------|
| 后端 | Python 3.10+，Flask，Flask-SocketIO，Eventlet |
| AI | OpenAI GPT API，Pydantic schema 结构化输出 |
| 牌局引擎 | 纯 Python，支持边池、全押、双次发牌 |
| 牌力评估 | [treys](https://github.com/ihendley/treys) |
| 前端 | 原生 JS，Socket.IO 客户端，CSS 变量 |
| 生产服务器 | Gunicorn + Eventlet worker |

---

## 目录结构

```
poker-ai/
├── main.py              # 开发模式入口 (python main.py)
├── wsgi.py              # 生产模式入口 (gunicorn wsgi:app)
├── game_config.yaml     # 服务端默认配置
├── requirements.txt
├── poker/               # 牌局引擎
│   ├── game_engine.py   # 状态机：发牌、下注、结算、双次发牌
│   ├── game_state.py    # 数据类：GameState, PlayerState
│   ├── hand_evaluator.py
│   ├── card.py
│   └── player.py
├── ai/                  # GPT 集成
│   ├── gpt_client.py    # OpenAI 客户端初始化
│   ├── opponent.py      # AI 对手决策（含风格 prompt）
│   ├── advisor.py       # 实时提示 + 赛后分析
│   └── schemas.py       # Pydantic 输出 schema
└── web/
    ├── server.py        # Flask 应用，REST + WebSocket 路由
    ├── templates/
    │   └── index.html
    └── static/
        ├── css/style.css
        └── js/app.js
```

---

## 安装与运行

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置 `game_config.yaml`

```yaml
ai:
  model: "gpt-4o"            # 填入实际模型名
  base_url: "https://..."    # OpenAI 或兼容 API 地址
  api_key: "sk-..."          # API 密钥
```

其余配置项见下方"配置说明"。

### 3. 启动

**开发模式**（本地调试）：
```bash
python main.py
```

**生产模式**（10~50 人并发）：
```bash
gunicorn --worker-class eventlet -w 1 --bind 0.0.0.0:5000 --timeout 120 wsgi:app
```

> `-w 1` 是必须的：游戏 session 存储在进程内存中，多 worker 会导致请求路由到不同进程。Eventlet 绿色线程足以支撑几十个并发用户（瓶颈在 AI API 延迟，不在服务器并发）。

浏览器打开 `http://localhost:5000` 即可游戏。

---

## 配置说明（`game_config.yaml`）

```yaml
game:
  mode: cash            # cash（现金局）| tournament（锦标赛）

table:
  num_opponents: 5      # AI 对手数量，2~5
  starting_chips: 2000  # 初始筹码（也是补码/重买的基准值）
  max_chips: 4000       # 筹码上限，超出时按 starting_chips 步长卸码；默认 2×starting_chips

blinds:
  small_blind: 10
  big_blind: 20
  ante: 0               # 0 = 无前注

ai:
  model: "gpt-4o"
  base_url: "https://api.openai.com/v1"
  api_key: "sk-..."
  response_delay: 0     # AI 响应延迟（秒），0 = 尽快响应

training:
  hint_enabled: true          # 实时提示功能
  post_hand_analysis: true    # 赛后 AI 分析
  show_opponent_styles: true  # 是否在桌面显示对手风格标签
  opponent_styles:            # 每个 AI 对手的风格，不足时循环使用
    - random                  # random | tight_aggressive | loose_aggressive
    - tight_aggressive        # tight_passive | loose_passive | balanced
    - loose_aggressive

features:
  run_it_twice: true    # 双方全押时是否提供双次发牌选项

auth:
  passwords: []         # 留空则不需要密码；填入后访问需要验证
    # - "your-password-here"
```

所有 `training`、`table`、`blinds`、`features` 下的参数均可在游戏内"游戏配置"弹窗中实时修改，配置保存于浏览器 localStorage，刷新不丢失。`ai` 和 `auth` 仅能通过配置文件修改。

---

## 游戏内配置弹窗

点击右上角"⚙️ 游戏配置"可调整：

| 区块 | 可配置项 |
|------|---------|
| 游戏模式 | 现金局 / 锦标赛 |
| 牌桌 | AI 对手数量、初始筹码、最大筹码上限 |
| 盲注 | 小盲、大盲、前注 |
| 训练功能 | 实时提示、赛后分析、显示对手风格 |
| 功能 | 双次发牌 |
| 玩家的筹码与风格 | 各玩家当前筹码（立即生效）、各 AI 对手风格 |

修改筹码并保存后，若当前有进行中的游戏，筹码会立即更新到桌面；若无游戏，保存的筹码将在下次"开始游戏"时作为初始值。

---

## AI 对手风格

| 风格 | 说明 |
|------|------|
| `random` | 每次随机分配一种风格 |
| `tight_aggressive` | 紧凶（TAG）：范围窄，入池后积极下注加注 |
| `loose_aggressive` | 松凶（LAG）：范围宽，频繁下注诈唬，主动制造压力 |
| `tight_passive` | 紧弱：只玩强牌，但倾向跟注而非加注 |
| `loose_passive` | 松弱：玩很多手牌，几乎只跟注 |
| `balanced` | 均衡（GTO）：混合策略，难以被读牌 |

---

## API 路由（供参考）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth` | 密码验证 |
| GET  | `/api/auth/status` | 检查认证状态 |
| POST | `/api/session/start` | 开始新游戏，接受完整配置参数 |
| POST | `/api/session/chips` | 直接修改当前 session 各玩家筹码 |
| POST | `/api/game/action` | 提交玩家行动（fold/check/call/raise/all_in） |
| POST | `/api/game/next-hand` | 开始下一手 |
| POST | `/api/game/hint` | 请求实时操作建议 |
| POST | `/api/game/analyze` | 请求赛后牌局分析 |
| POST | `/api/game/run-it-twice` | 提交双次发牌决定 |
| POST | `/api/game/run-it-twice-hint` | 请求双次发牌建议 |

WebSocket 事件由 Flask-SocketIO 推送：`state_update`、`hand_result`、`rit_request`。

---

## Nginx 反向代理（可选）

若需要 HTTPS 或域名访问，在 Gunicorn 前加 Nginx：

```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 120s;
    }
}
```

WebSocket 升级依赖 `Upgrade` / `Connection` 两个 header，缺少时长连接会失败。
