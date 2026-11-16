#!/usr/bin/env python3
"""aa-notify：把 Agents Anywhere 的会话事件推到微信。

AA 的 iOS/Android/Web 端目前没有任何推送能力（上游 issue #268），
桌面端只有 Electron 系统通知。本脚本常驻本机，用桌面端已登录的凭据
轮询 /api/v2/sessions，在「需要审批 / 回合结束未读 / 出错」时推送。

只读 AA 本地登录态，不修改 AA 的任何数据；无第三方依赖，Python 3.9+。
"""

from __future__ import annotations

import argparse
import base64
import calendar
import fcntl
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "state.json")
LOG_PATH = os.path.join(HERE, "aa-notify.log")
LOCK_PATH = os.path.join(HERE, "aa-notify.lock")

AA_SUPPORT = os.path.expanduser("~/Library/Application Support/Agents Anywhere")
LEVELDB_DIR = os.path.join(AA_SUPPORT, "Local Storage", "leveldb")
SERVER = "https://web.agents-anywhere.com"
UA = "aa-notify/1.0"

BUSY_STATUSES = {"running", "waiting", "pending", "stopping"}

DEFAULT_CONFIG = {
    "channel": "pushplus",
    "pushplusToken": "",
    "wxpusher": {"appToken": "", "uids": []},
    "serverChanKey": "",
    "linkBase": "https://web.agents-anywhere.com",
    "pollSeconds": 20,
    "quietHours": {"start": "00:00", "end": "08:00"},
    "onlyWhenAway": {"enabled": True, "idleSeconds": 180},
    "idleStopMinutes": 0,
    "dormantPollSeconds": 300,
    "events": {"approval": True, "completed": True, "error": True},
    "dedupeSeconds": 120,
    "maxPerHour": 30,
}


# ---------------------------------------------------------------- 基础设施


def log(message: str) -> None:
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
    print(line, flush=True)
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 2 * 1024 * 1024:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def parse_timestamp(text: str) -> float:
    if not text:
        return 0.0
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return float(calendar.timegm(time.strptime(text, fmt)))
        except ValueError:
            continue
    return 0.0


def _minutes(text: str) -> int:
    hour, _, minute = str(text).partition(":")
    return int(hour) * 60 + int(minute or 0)


def in_quiet_hours(config: dict, now: float | None = None) -> bool:
    quiet = config.get("quietHours") or {}
    start, end = quiet.get("start"), quiet.get("end")
    if not start or not end:
        return False
    current = time.localtime(now or time.time())
    cur = current.tm_hour * 60 + current.tm_min
    first, last = _minutes(start), _minutes(end)
    if first == last:
        return False
    return (first <= cur < last) if first < last else (cur >= first or cur < last)


def seconds_until_quiet_end(config: dict, now: float | None = None) -> int:
    stamp = now or time.time()
    current = time.localtime(stamp)
    midnight = time.mktime((current.tm_year, current.tm_mon, current.tm_mday, 0, 0, 0, 0, 0, -1))
    target = midnight + _minutes((config.get("quietHours") or {}).get("end") or "08:00") * 60
    return max(60, int(target - stamp))


def load_config(path: str) -> dict:
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            user = json.load(handle)
        for key, value in user.items():
            if isinstance(value, dict) and isinstance(config.get(key), dict):
                config[key].update(value)
            else:
                config[key] = value
    return config


def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {"sessions": {}, "lastNotified": {}, "sent": []}


def save_state(state: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_PATH)


# ---------------------------------------------------------------- AA 登录态


def _jwt_payload(token: str):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return None


def mac_idle_seconds() -> float | None:
    """本机键鼠空闲秒数；拿不到返回 None（按“不在场”处理，宁可多推）。"""
    try:
        output = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem", "-d", "4"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    values = [int(value) for value in re.findall(r'"HIDIdleTime"\s*=\s*(\d+)', output)]
    if not values:
        return None
    return max(values) / 1e9


def find_user_token() -> str | None:
    """从 AA 桌面端 Local Storage 里取未过期的用户 access token。"""
    best = None  # (exp, token)
    pattern = re.compile(rb"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")
    for path in glob.glob(os.path.join(LEVELDB_DIR, "*")):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as handle:
                blob = handle.read()
        except OSError:
            continue
        for match in pattern.finditer(blob):
            token = match.group().decode("ascii", "ignore")
            payload = _jwt_payload(token)
            if not payload or not str(payload.get("sub", "")).startswith("usr_"):
                continue
            exp = int(payload.get("exp") or 0)
            if exp and exp < time.time() + 120:
                continue
            if best is None or exp > best[0]:
                best = (exp, token)
    return best[1] if best else None


class ApiError(Exception):
    pass


class Api:
    def __init__(self) -> None:
        self._token = None
        self._projects: dict[str, str] = {}
        self._projects_at = 0.0

    def token(self) -> str:
        if not self._token:
            self._token = find_user_token()
        if not self._token:
            raise ApiError("找不到 AA 桌面端登录态（先打开一次 Agents Anywhere）")
        return self._token

    def get(self, path: str):
        for attempt in (0, 1):
            request = urllib.request.Request(
                SERVER + path,
                headers={"Authorization": "Bearer " + self.token(), "User-Agent": UA},
            )
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                if error.code == 401 and attempt == 0:
                    self._token = None  # 桌面端刷新过 token，重扫一次
                    continue
                raise ApiError("GET %s -> HTTP %s" % (path, error.code))
            except (urllib.error.URLError, TimeoutError, ValueError) as error:
                raise ApiError("GET %s -> %s" % (path, error))
        raise ApiError("GET %s -> 未授权" % path)

    def sessions(self) -> list[dict]:
        return self.get("/api/v2/sessions").get("sessions") or []

    def open_notices(self, session_id: str) -> list[dict]:
        """仍在等用户响应的 notice（审批、选择题、确认）。"""
        try:
            notices = self.get("/api/v2/sessions/%s/runtime/notices" % session_id).get("notices") or []
        except ApiError:
            return []
        return [
            notice
            for notice in notices
            if notice.get("status") == "open" and notice.get("responseRequired")
        ]

    def approval_detail(self, session_id: str) -> str:
        """取当前待处理审批的摘要，避免每条通知内容一模一样。"""
        lines = []
        for notice in self.open_notices(session_id)[:2]:
            text = " ".join(((notice.get("message") or notice.get("title") or "").strip()).split())
            if text:
                lines.append(text[:120])
        return "\n".join(lines)

    def project_name(self, project_id: str) -> str:
        if time.time() - self._projects_at > 600:
            try:
                for item in self.get("/api/v2/projects").get("projects") or []:
                    self._projects[item["id"]] = item.get("name") or "未命名项目"
                self._projects_at = time.time()
            except ApiError:
                pass
        return self._projects.get(project_id, "")


# ---------------------------------------------------------------- 推送通道


def http_post(url: str, payload: dict, form: bool = False) -> tuple[int, str]:
    if form:
        body = urllib.parse.urlencode(payload).encode("utf-8")
        content_type = "application/x-www-form-urlencoded"
    else:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        content_type = "application/json"
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": content_type, "User-Agent": UA}
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read().decode("utf-8", "replace")[:300]
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")[:300]
    except (urllib.error.URLError, TimeoutError) as error:
        return 0, str(error)


def send_push(config: dict, title: str, content: str) -> tuple[bool, str]:
    channel = config.get("channel")
    if channel == "pushplus":
        token = config.get("pushplusToken")
        if not token:
            return False, "pushplusToken 未配置"
        status, body = http_post(
            "https://www.pushplus.plus/send",
            {"token": token, "title": title, "content": content, "template": "txt"},
        )
        return status == 200 and '"code":200' in body.replace(" ", ""), body
    if channel == "wxpusher":
        app_token = (config.get("wxpusher") or {}).get("appToken")
        uids = (config.get("wxpusher") or {}).get("uids") or []
        if not app_token or not uids:
            return False, "wxpusher appToken/uids 未配置"
        status, body = http_post(
            "https://wxpusher.zjiecode.com/api/send/message",
            {
                "appToken": app_token,
                "content": content,
                "summary": title,
                "contentType": 1,
                "uids": uids,
            },
        )
        return status == 200 and '"code":1000' in body.replace(" ", ""), body
    if channel == "serverchan":
        key = config.get("serverChanKey")
        if not key:
            return False, "serverChanKey 未配置"
        status, body = http_post(
            "https://sctapi.ftqq.com/%s.send" % key,
            {"title": title, "desp": content},
            form=True,
        )
        return status == 200 and '"code":0' in body.replace(" ", ""), body
    return False, "未知通道 %r" % channel


# ---------------------------------------------------------------- 事件判定


def session_event(previous: dict | None, current: dict) -> str | None:
    if previous is None:
        return None
    status = current.get("status")
    previous_status = previous.get("status")
    turn = int(current.get("latestTurnEndSeq") or 0)
    previous_turn = int(previous.get("latestTurnEndSeq") or 0)
    if status == "waiting_approval" and previous_status != "waiting_approval":
        return "approval"
    if status in ("error", "blocked") and (
        previous_status != status or (not previous.get("unread") and current.get("unread"))
    ):
        return "error"
    if status == "idle" and current.get("unread") and (
        previous_status in BUSY_STATUSES or turn > previous_turn
    ):
        return "completed"
    return None


HEADING = {"approval": "待你审批", "completed": "回合结束", "error": "出错"}


def summarize(session: dict, project: str = "") -> str:
    lines = []
    if project:
        lines.append("项目：" + project)
    runtime = session.get("runtimeName") or session.get("runtime") or ""
    if runtime:
        lines.append("运行时：" + str(runtime))
    return "\n".join(lines)


def handle_event(api: Api, config: dict, state: dict, session: dict, kind: str) -> bool:
    now = time.time()
    session_id = session["id"]
    sent = [stamp for stamp in state.get("sent", []) if now - stamp < 3600]
    state["sent"] = sent
    if len(sent) >= int(config.get("maxPerHour") or 30):
        log("跳过 %s：一小时推送已达上限" % session_id)
        return False
    event_key = "%s:%s:%s" % (kind, session.get("updatedSeq"), session.get("latestTurnEndSeq"))
    last = state["lastNotified"].get(session_id) or {}
    if last.get("key") == event_key and now - float(last.get("ts") or 0) < int(config.get("dedupeSeconds") or 120):
        log("跳过 %s：同一事件已推送过" % session_id)
        return False

    gate = config.get("onlyWhenAway") or {}
    if gate.get("enabled", True):
        idle = mac_idle_seconds()
        threshold = float(gate.get("idleSeconds") or 300)
        if idle is not None and idle < threshold:
            log("跳过 %s：本机正在使用（空闲 %.0f 秒），桌面端已经弹过" % (session_id, idle))
            state["lastNotified"][session_id] = {"kind": kind, "key": event_key, "ts": now}
            return False

    project = api.project_name(session.get("projectId") or "")
    subject = (session.get("title") or "未命名会话").strip()
    if len(subject) > 26:
        subject = subject[:26] + "…"
    title = "%s：%s" % (HEADING.get(kind, "通知"), subject)

    link = "%s/#/session/%s" % (str(config.get("linkBase") or SERVER).rstrip("/"), session_id)
    body_lines = [summarize(session, project)]
    if kind == "approval":
        detail = api.approval_detail(session_id)
        if detail:
            body_lines.append(detail)
        body_lines.append("在微信里打开链接，登录后即可批准/拒绝")
    body_lines.append(time.strftime("%H:%M:%S"))
    body_lines.append(link)
    body = "\n".join(line for line in body_lines if line)

    if config.get("_dry_run"):
        log("[dry-run] %s | %s | %s" % (kind, title, (subject)))
        ok = True
    else:
        ok, detail = send_push(config, title, body)
        log("%s推送 %s %s | %s" % ("已" if ok else "失败", kind, session_id, "" if ok else detail))
        if not ok:
            return False
    state["lastNotified"][session_id] = {"kind": kind, "key": event_key, "ts": now}
    sent.append(now)
    return True


def poll_once(api: Api, config: dict, state: dict, notify: bool = True) -> int:
    sessions = api.sessions()
    by_id = {item["id"]: item for item in sessions if item.get("id")}
    touched = 0

    # 上一轮挂起的「回合结束」：隔一轮再看是否其实在等用户响应，
    # 等响应就不叫「结束」，改发待审批。
    deferred = state.setdefault("deferred", {})
    if not notify:
        deferred.clear()
    for session_id, item in list(deferred.items()):
        if time.time() - float(item.get("at") or 0) < 15:
            continue
        deferred.pop(session_id, None)
        session = by_id.get(session_id)
        if not session or session.get("archived") or session.get("userArchived"):
            continue
        if session.get("status") == "waiting_approval":
            continue  # 状态事件自己会发审批
        kind = "approval" if api.open_notices(session_id) else "completed"
        if (config.get("events") or {}).get(kind, True) and handle_event(api, config, state, session, kind):
            touched += 1

    latest_activity = max(
        (
            max(
                parse_timestamp(item.get("lastItemAt") or ""),
                parse_timestamp(item.get("lastActivityAt") or ""),
                parse_timestamp(item.get("sourceObservedAt") or ""),
            )
            for item in sessions
        ),
        default=0.0,
    )
    if any(item.get("status") in BUSY_STATUSES or item.get("status") == "waiting_approval" for item in sessions):
        latest_activity = max(latest_activity, time.time())
    state["activityTs"] = latest_activity

    seen = set()
    for session in sessions:
        if session.get("archived") or session.get("userArchived"):
            continue
        session_id = session.get("id")
        if not session_id:
            continue
        seen.add(session_id)
        snapshot = {
            "status": session.get("status"),
            "unread": bool(session.get("unread")),
            "turn": int(session.get("latestTurnEndSeq") or 0),
        }
        previous = state["sessions"].get(session_id)
        kind = session_event(previous, snapshot | {"id": session_id})
        state["sessions"][session_id] = snapshot
        if not kind:
            continue
        if not (config.get("events") or {}).get(kind, True):
            continue
        if not notify:
            continue
        if kind == "completed":
            deferred[session_id] = {"at": time.time()}
            continue
        if handle_event(api, config, state, session, kind):
            touched += 1
    for session_id in list(state["sessions"]):
        if session_id not in seen:
            state["sessions"].pop(session_id, None)
    return touched


def notify_waiting_approvals(api: Api, config: dict, state: dict) -> int:
    """安静时段结束后，把仍卡在审批上的会话各补一条。"""
    touched = 0
    if not (config.get("events") or {}).get("approval", True):
        return 0
    checks = 0
    for session in api.sessions():
        if session.get("archived") or session.get("userArchived"):
            continue
        waiting = session.get("status") == "waiting_approval"
        if not waiting:
            if not session.get("unread") or checks >= 5:
                continue
            checks += 1
            waiting = bool(api.open_notices(session.get("id") or ""))
        if waiting and handle_event(api, config, state, session, "approval"):
            touched += 1
    return touched


# ---------------------------------------------------------------- 入口


def main() -> int:
    parser = argparse.ArgumentParser(description="Agents Anywhere → 微信通知桥")
    parser.add_argument("--config", default=os.path.join(HERE, "config.json"))
    parser.add_argument("--once", action="store_true", help="只轮询一次")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不推送")
    args = parser.parse_args()

    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("已有实例在运行，退出")
        return 1

    config = load_config(args.config)
    config["_dry_run"] = args.dry_run
    state = load_state()
    api = Api()
    interval = max(5, int(config.get("pollSeconds") or 20))
    log("启动：channel=%s interval=%ss dry_run=%s" % (config.get("channel"), interval, args.dry_run))

    quiet = False
    dormant = False
    resume_baseline = False
    while True:
        if in_quiet_hours(config):
            if not quiet:
                window = config.get("quietHours") or {}
                log("安静时段，暂停轮询（%s-%s）" % (window.get("start"), window.get("end")))
                quiet = True
                resume_baseline = True
            if args.once:
                return 0
            time.sleep(min(600, seconds_until_quiet_end(config)))
            continue
        if quiet:
            log("离开安静时段，恢复轮询")
            quiet = False
        try:
            if resume_baseline:
                poll_once(api, config, state, notify=False)
                save_state(state)
                resume_baseline = False
                log("夜间基线已更新，补发待审批 %d 条" % notify_waiting_approvals(api, config, state))
                save_state(state)
                if args.once:
                    return 0
                continue
            touched = poll_once(api, config, state)
            save_state(state)
            window = int(config.get("idleStopMinutes") or 0) * 60
            idle_for = time.time() - float(state.get("activityTs") or 0)
            if window > 0 and idle_for > window and not dormant:
                log("最近 %d 分钟没有新活动，停止高频轮询（每 %d 秒探一次）" % (window // 60, int(config.get("dormantPollSeconds") or 300)))
                dormant = True
            elif dormant and (window <= 0 or idle_for <= window):
                log("检测到新活动，恢复 %d 秒轮询" % interval)
                dormant = False
            if args.once:
                log("单次轮询完成，事件 %d 条" % touched)
                return 0
        except ApiError as error:
            log("轮询失败：%s" % error)
            if args.once:
                return 2
        except Exception as error:  # noqa: BLE001 - 常驻进程不能因单次异常退出
            log("未预期错误：%r" % error)
            if args.once:
                return 3
        time.sleep(int(config.get("dormantPollSeconds") or 300) if dormant else interval)


if __name__ == "__main__":
    sys.exit(main())
