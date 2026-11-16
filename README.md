# aa-notify

把 Agents Anywhere 的会话事件推到微信。AA 的 iOS/Android/Web 端没有推送能力（上游 [#268](https://github.com/anywhere-labs/Agents-Anywhere/issues/268)），桌面端只有 Mac 本地通知；本服务补「人不在电脑前」这一段。

只读 AA 桌面端本地登录态，不改 AA 数据、不碰 connector；单文件 Python，无第三方依赖。

## 触发点

| 事件 | 判定 |
|---|---|
| 待你审批 | 会话进入 `waiting_approval`，或回合结束后仍有 `responseRequired` 的 notice |
| 回合结束 | 忙碌态 → `idle`、有未读、`latestTurnEndSeq` 增加，且没有待响应的 notice；会晚一轮（约 20 秒）再确认一次 |
| 出错 | 进入 `error`/`blocked`，或错误态出现新未读 |

标题分别是「待你审批：<会话标题>」「回合结束：<会话标题>」「出错：<会话标题>」，正文再给项目、运行时、具体请求（如 `请求批准：bash …`）、时间和链接。审批没处理完的会话不会被标成「回合结束」。

同一事件不重复推（同一会话同一次状态变化 120 秒内只发一条），每小时最多 30 条；首次启动只记基线，不补推历史。

## 配置

```bash
cp config.example.json config.json
```

`channel` 三选一：

- `pushplus`：填 `pushplusToken`（[pushplus.plus](https://www.pushplus.plus) 微信扫码登录后复制 token），微信渠道免费。
- `wxpusher`：填 `wxpusher.appToken` 和 `wxpusher.uids`（[wxpusher](https://wxpusher.zjiecode.com) 建应用 + 关注公众号拿 UID），永久免费。
- `serverchan`：填 `serverChanKey`（[sct.ftqq.com](https://sct.ftqq.com) 微信扫码，SENDKEY 以 `SCT` 开头），免费 5 条/天。

微信侧收到的都是公众号「服务通知」，不是聊天消息；点开正文里的链接可跳转控制台。

## 运行

**已部署**：2026-11-15，通道 `pushplus`，LaunchAgent `com.rain.aa-notify`（`RunAtLoad` + `KeepAlive`），日志见 `aa-notify.log`。

```bash
/usr/bin/python3 aa_notify.py --once --dry-run   # 只打印，验证能读到会话
/usr/bin/python3 aa_notify.py                    # 前台常驻
```

常驻（开机自启）：

```bash
cp com.rain.aa-notify.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.rain.aa-notify.plist
launchctl kickstart -k gui/$(id -u)/com.rain.aa-notify
```

plist 在部署根 `~/rain/projs/services/aa-notify/` 里。

## 部署与文件归属

源码在本目录，运行时在部署根 `~/rain/projs/services/aa-notify/`。改完代码从 `apps/` 侧推过去，**不要直接改部署根**：

```bash
cd ~/rain/projs && ./deploy.sh aa-notify
```

| 文件 | 归属 |
|---|---|
| `aa_notify.py`、`README.md`、`config.example.json` | 源码，本目录；部署时单向覆盖到部署根 |
| `config.json`、`state.json`、`aa-notify.log`、`aa-notify.lock`、`launchd.*.log` | 运行时，只存在于部署根；部署永不覆盖 |
| `com.rain.aa-notify.plist` | 部署根持有，本目录不存 |

改完代码要生效需重启进程（Python 已把旧文件读进内存）：`launchctl kickstart -k gui/$(id -u)/com.rain.aa-notify`。

## 只在离开电脑时推

`onlyWhenAway` 默认开启：发推送前读本机键鼠空闲时间（`ioreg` 的 `HIDIdleTime`），空闲不足 `idleSeconds`（默认 180 秒）就直接跳过——人在电脑前，AA 桌面端本来就会弹系统通知，微信再响一遍是噪音；锁屏、离开 3 分钟以上才推微信。

AA 没有暴露「手机端在线」的接口（`/api/v2/devices`、`/mobile-connections` 等路径返回的都是前端 HTML），所以用本机空闲时间做代理，不是真的在检测手机。读不到空闲时间时按「不在场」处理，宁可多推。

## 安静时段

`config.json` 的 `quietHours` 默认 `00:00`–`08:00`：这段时间**完全不轮询**（不是只静音），进程只按小时级 sleep 等结束。支持跨零点，例如 `{"start": "22:00", "end": "06:00"}`；留空或删掉即全天轮询。

结束时先做一次静默轮询重建基线（不补发整夜的旧变化），再单独补发仍在 `waiting_approval` 或有待响应 notice 的会话。

## 活动门控（休眠）

默认关闭（`idleStopMinutes: 0`），保持 20 秒轮询。这条是为 API 探针提的需求，消息通知不适用：审批通知的价值就在于人不在场时也能收到，停轮询会漏。需要时再开，语义是「所有会话最近活动超过 N 分钟就降到 `dormantPollSeconds` 探一次」。

活动时间取 `lastItemAt`、`lastActivityAt`、`sourceObservedAt` 三者的最大值，任一会话处于 `running`/`waiting`/`waiting_approval` 也算活跃。注意 **`lastActivityAt` 会长期陈旧**（实测可达 9 小时），只拿它判断会把正在跑的会话误判成休眠。

进程不退出（launchd 仍托管），只是把请求量从 180 次/小时降到 12 次/小时；想彻底零请求只能停服务，那样也没法自动恢复。

## 状态与日志

- `state.json`：会话基线、最近推送记录；删掉即重置（下一次只记基线）。
- `aa-notify.log`：滚动日志，超过 2 MB 轮转一份 `.1`。
- 停止与回滚：`launchctl bootout gui/$(id -u)/com.rain.aa-notify`，删 plist 即恢复原状；不涉及系统配置。

## 登录态

token 从 `~/Library/Application Support/Agents Anywhere/Local Storage/leveldb` 里读，取未过期、`sub=usr_*` 的那条；过期（约 7 天）后由 AA 桌面端自己刷新，脚本每轮重读一次，读不到就每 20 秒重试并记日志。**AA 桌面端要开着**，否则没有 token 也没有实时会话状态。

## 已知边界

- 轮询 20 秒，最快延迟约 20 秒；不是服务端推送。
- PushPlus 对「相同内容」有 1 小时 3 条限制，超发返回 `code 999` 且不投递；所以每条正文都带时间和审批摘要，不要改成固定文案。
- AA 会话在别的设备上归档不影响；本机归档的会话不提醒。
- 不发布、不推广：依赖 AA 桌面端私有存储路径，AA 升级可能失效。
