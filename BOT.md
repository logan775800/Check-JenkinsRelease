# jenkins-bot（Slack 发版机器人）

在 Slack 频道里按站点发布 Jenkins 服务：贴发布单 → @ 站点编号 → 核对计划 → 点「确认发布」→ 机器人逐个触发 job、跟踪到结束、按 **Git 实际拉到的分支** 核对并回结论。

和网页版（`app.py`）共用同一套解析与核对代码，两边口径一致。

---

## 文件

| 文件 | 作用 |
|---|---|
| `bot.py` | 机器人本体（Slack 事件、计划、触发、跟踪、看板、记录） |
| `app.py` | 网页版；机器人 `import app` 复用发布单解析、站点/组件识别、`run_check` 核对、Jenkins 访问 |
| `Dockerfile.bot` | 机器人镜像（网页版坚持零依赖，机器人要 `slack_bolt`，所以单独一个镜像） |
| `docker-compose.yml` | 两个服务：`jenkins-check`（网页）、`jenkins-bot`（机器人） |

## 依赖

- Python 3.12、`slack_bolt>=1.18,<2`（镜像里装）
- Slack App：**Socket Mode**（机器人主动连 Slack，服务器不开端口、不需要公网回调）
  - Bot scopes：`app_mentions:read` `chat:write` `channels:history` `groups:history` `im:history`
  - Events：`app_mention` `message.im`；Interactivity 打开（按钮）
  - **不要开 Distribute App（公开分发）**
- Jenkins：AR 那套 + 后台（Admin）那套，凭据沿用网页版 `.env`；触发用的账号需要 Build 权限
- 服务器出口 IP 要在两套 Jenkins 前面的 WAF 白名单里

## 配置

两份 env，都**不进仓库**：

| 变量 | 放在 | 说明 |
|---|---|---|
| `JENKINS_URL` `JENKINS_USER` `JENKINS_API_TOKEN` | 仓库目录 `.env` | 和网页版共用 |
| `ADMIN_JENKINS_URL` `ADMIN_JENKINS_USER` `ADMIN_JENKINS_TOKEN` | 仓库目录 `.env` | 后台那套；不配则后台服务不发 |
| `SLACK_APP_TOKEN`（xapp-）`SLACK_BOT_TOKEN`（xoxb-） | 仓库外 `/data/release-bot/.env` | chmod 600 |
| `BOT_ALLOWED_USERS` | 仓库外 `/data/release-bot/.env` | 能点「确认发布」的 Slack user id，逗号分隔。**空 = 谁都不能发**（安全默认）。加人 = 给他生产发布权限 |
| `BOT_TZ` | compose | 消息里的时间时区，默认 `Asia/Dubai`（UTC+4） |
| `BOT_STALE_HOURS` | 环境变量 | 服务清单多久没更新就不给确认按钮，默认 12 |
| `BOT_BOARD_EVERY` | 环境变量 | 有站点在发布时看板刷新间隔（秒），默认 60 |
| `BOT_STATE` | compose | 状态文件路径，容器内 `/state/state.json` |
| `BOT_OFFLINE` | **只在本地测试用** | =1 时不向 Slack 校验 token；线上别设 |

## 状态与数据（容器内 `/state` = 宿主机 `/data/release-bot/state`）

| 文件 | 内容 | 丢了会怎样 |
|---|---|---|
| `state.json` | 每个频道的服务清单、站点清单、看板消息 ts；站点分组（中台/非中台）；**正在发布的站点 `running`** | 要重发服务/站点清单；正在发布的站重启后无法续跟 |
| `releases.jsonl` | 每次确认/触发/结果一行，追加写，永久保留（`@jenkins-bot 记录 AR035` 读它） | 历史记录没了，不影响发布 |

只在内存里的：未确认的计划（重启后旧按钮失效，重新 @ 即可）、build_admin 在途锁。

## 关键行为（改代码前先知道）

- **触发构建一律显式传参**（`buildWithParameters?TAG=…` / `BRANCH_NAME=…`），不用 job 默认值；分支简写（`v3-uat`）先在 Jenkins 历史值里唯一匹配成全名（`feature/v3-uat`），匹配不上或多个就拒发。
- **核对比 Git 实际拉到的分支**（`lastBuiltRevision`），不比自己传的参数（比参数永远相等）。
- **按视图发**（`web ar107 [分支]`）：`parse_view_cmd` 只认精确别名；必须在「服务清单」判断**之前**解析——发布单解析会把 `web ar107` 当成一行「web → ar107」把清单改坏。带分支 = 临时用，不改清单、不受过期限制。
- **没有分支参数的 job**（RebateJob 全部 78 个，分支写死在流水线里）：要求的分支 = 它上次实际拉的分支才发（POST `/build` 不传参），发完照样按 Git 实际分支核对；不一致就拒发并说明。
- **中台/非中台不靠猜**：来自站点清单的分组标题；没有就问。标题后面可以带说明（`中台站（34个）   feature/v3-uat …`）。
- **build_admin 同一版本只打一次**：最近成功构建已是该版本就跳过；否则机器人触发一次（参数 `TAG`），全局锁 + 在途记录防两站并发重复打包；有包后才发本站后台部署 job。
- **发布中重启**：`running` 每触发一个 job 就落盘；启动时 `resume_all` 接着跟踪。已触发但没记下构建号的按「确认之后开始的构建」找回；**还没触发的不补触发**（宁可漏报不重复发版）。
- 失败只附报错行（排除 warning、`0 Error(s)`、`[Pipeline]` 收尾行）；「重发失败的」只包含构建失败/触发失败/被取消/后台没发成，不含发错、超时、跳过。
- Slack 文本坑：粘贴多行会被包成代码块（首末行带 ```）、`>` 变 `&gt;`，`strip_mention` 已处理；**粗体右边 `*` 后紧跟中文标点（，：（）不生效**，显示成星号，写文案留空格。

## 部署（安全做法）

**部署前必须确认没有站点在发布**，否则重启会打断跟踪（虽然能续跟，别赌）：

```sh
cd /data/Check-JenkinsRelease
python -c "import json;print(json.load(open('/data/release-bot/state/state.json')).get('running',{}).keys())"   # 必须是空
git pull
docker compose build jenkins-bot          # 先构建，不影响正在跑的容器
docker compose up -d jenkins-bot          # 切换只要几秒
docker logs --tail 5 jenkins-bot          # 看到 "Bolt app is running"
```

发版高峰一直有站点在发时：先 `build`，再用一个循环每 30 秒查一次 `running`，空了立刻 `up -d`。

只改了网页版（`app.py` 页面部分）时用 `docker compose up -d --build jenkins-check`，不碰机器人。**注意：机器人镜像里也有一份 `app.py`**，改了解析/核对逻辑要两个都重建。

## 测试

```sh
python tools/selftest.py      # 网页版/解析的离线回归（改 app.py 必跑）
python tools/checkjs.py       # 网页内联 JS 检查
```

机器人没有自动化测试，本地这样测（不连 Slack，可只读访问真实 Jenkins）：

```python
import os
os.environ.update(SLACK_BOT_TOKEN="xoxb-fake", BOT_OFFLINE="1", BOT_STATE="local/botstate/t.json")
# JENKINS_* 按需填；把 bot.jpost / bot.jget / bot.jenkins_index 换成假的就能离线跑完整发布流程
import bot
print(bot.handle("<@X> 服务\nV1-LotteryApi → 分支：master_V3.11_015", "C", "U"))
```

**别在测试里把 `jpost` 留成真的**——那会真的触发生产构建。

## 回滚

```sh
cd /data/Check-JenkinsRelease
# 先确认 running 为空（同上）
cp /data/release-bot/state/state.json /data/release-bot/state/state.json.bak-$(date +%m%d%H%M)
git log --oneline -8 -- bot.py            # 找要回到的提交
git checkout <提交号>                       # 进入 detached HEAD
docker compose up -d --build jenkins-bot
# 回到最新：git checkout main && git pull && docker compose up -d --build jenkins-bot
```

回滚救不了的：**已经触发的构建撤不回来**——发错的站只能用正确分支再发一次；`state.json` 结构如果被新版本改过，旧版本读不了时用备份恢复。
